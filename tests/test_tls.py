"""Real TLS tests: authentication must stay behind certificate verification."""

import asyncio
import hashlib
import inspect
import ssl
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from pathlib import Path
from unittest.mock import Mock, patch

from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from grpclib.client import Channel
from grpclib.const import Cardinality, Handler
from grpclib.server import Server

import PasarGuardNodeBridge as Bridge
from PasarGuardNodeBridge.common import service_pb2 as service
from PasarGuardNodeBridge.controller import NodeAPIError
from PasarGuardNodeBridge.grpclib import Node as GrpcNode
from PasarGuardNodeBridge.rest import Node as RestNode
from PasarGuardNodeBridge.tls import TLSPolicy, normalize_fingerprint

try:
    import uvloop
except ImportError:
    uvloop = None

KEY = "00000000-0000-0000-0000-000000000000"


def certificate(directory, *, identity="127.0.0.1", dates="valid", san=True):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.now(UTC)
    before, after = now - timedelta(days=1), now + timedelta(days=1)
    if dates == "expired":
        after = now - timedelta(seconds=1)
    elif dates == "short":
        after = now + timedelta(seconds=2)
    elif dates == "future":
        before, after = now + timedelta(days=1), now + timedelta(days=2)
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(before)
        .not_valid_after(after)
    )
    if san:
        try:
            identity_san = x509.IPAddress(ip_address(identity))
        except ValueError:
            identity_san = x509.DNSName(identity)
        builder = builder.add_extension(x509.SubjectAlternativeName([identity_san]), False)
    cert = builder.sign(key, hashes.SHA256())
    pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    cert_path, key_path = Path(directory) / "cert.pem", Path(directory) / "key.pem"
    cert_path.write_text(pem)
    key_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    ctx.set_alpn_protocols(["h2", "http/1.1"])
    pin = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    return ctx, pem, pin


class PolicyTests(unittest.TestCase):
    def test_non_weakref_transport_expiry_and_close(self):
        class Transport:
            __slots__ = ("aborted", "closed")

            def __init__(self):
                self.closed = False
                self.aborted = False

            def is_closing(self):
                return self.closed

            def abort(self):
                self.aborted = True

        for close_early in (False, True):
            with self.subTest(close_early=close_early):
                transport = Transport()
                loop = Mock()
                loop.time.return_value = 100.0
                expiry = datetime.now(UTC) + timedelta(days=30)
                policy = TLSPolicy.resolve(None, "ab" * 32)
                with (
                    patch.object(TLSPolicy, "verify", return_value=expiry),
                    patch("PasarGuardNodeBridge.tls.asyncio.get_running_loop", return_value=loop),
                ):
                    policy.guard(transport, "localhost")
                delay, callback, *args = loop.call_later.call_args.args
                self.assertEqual(delay, 1.0)
                deadline = args[-1]
                # A live connection keeps its expiry check scheduled.
                loop.call_later.reset_mock()
                loop.time.return_value = deadline - 0.25
                callback(*args)
                self.assertFalse(transport.aborted)
                self.assertEqual(loop.call_later.call_args.args[0], 0.25)
                loop.call_later.reset_mock()
                transport.closed = close_early
                loop.time.return_value = deadline
                callback(*args)
                self.assertEqual(transport.aborted, not close_early)
                loop.call_later.assert_not_called()

    def test_normalization(self):
        self.assertEqual(normalize_fingerprint(":".join(["AB"] * 32)), "ab" * 32)
        self.assertEqual(normalize_fingerprint("AB" * 32), "ab" * 32)
        for value in ("aa", " " * 64, ":".join(["a"] * 64), "zz" * 32, 4):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_fingerprint(value)

    def test_api_key_is_required(self):
        for constructor in (RestNode, GrpcNode, Bridge.create_node, Bridge.NodeConfig):
            parameters = inspect.signature(constructor).parameters
            self.assertIs(parameters["api_key"].default, inspect.Parameter.empty)
            self.assertIsNone(parameters["server_ca"].default)
            self.assertIs(parameters["server_ca"].kind, inspect.Parameter.KEYWORD_ONLY)
            self.assertLess(list(parameters).index("api_key"), list(parameters).index("server_ca"))

    def test_factory_and_registry_serialization(self):
        config = Bridge.NodeConfig("rest", "localhost", 1, 2, api_key=KEY, certificate_sha256="AB" * 32)
        restored = Bridge.NodeConfig.from_dict(config.to_dict())
        node = Bridge.create_node_from_config(restored)
        self.assertEqual(node._tls_policy.certificate_sha256, "ab" * 32)
        self.assertEqual(node._tls_policy.server_ca, "")
        legacy = Bridge.NodeConfig.from_dict(
            {"connection": "rest", "address": "localhost", "port": 1, "api_port": 2, "server_ca": "", "api_key": KEY}
        )
        self.assertIsNone(legacy.certificate_sha256)
        for node_class in (RestNode, GrpcNode):
            with self.assertRaises(NodeAPIError):
                node_class("localhost", 1, 2, api_key="")

    def test_dns_and_ipv6_sans(self):
        from PasarGuardNodeBridge.tls import TLSVerificationError

        for identity, matches, rejects in [
            ("*.example.com", ["node.example.com", "NODE.EXAMPLE.COM"], ["example.com", "a.b.example.com"]),
            ("xn--bcher-kva.example", ["bücher.example"], ["other.example"]),
            ("::1", ["::1", "0:0:0:0:0:0:0:1"], ["127.0.0.1"]),
        ]:
            with tempfile.TemporaryDirectory() as directory:
                _, pem, pin = certificate(directory, identity=identity)
                der = ssl.PEM_cert_to_DER_cert(pem)

                class Peer:
                    def getpeercert(self, binary_form, encoded=der):
                        return encoded

                class Transport:
                    def get_extra_info(self, name):
                        return Peer()

                policy = TLSPolicy.resolve(None, pin)
                for host in matches:
                    policy.verify(Transport(), host)
                for host in rejects:
                    with self.assertRaises(TLSVerificationError):
                        policy.verify(Transport(), host)

    def test_selection_and_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            _, pem, pin = certificate(directory)
            for ca, fingerprint, mode in [
                (None, None, ssl.CERT_REQUIRED),
                (pem, None, ssl.CERT_REQUIRED),
                (None, pin, ssl.CERT_NONE),
                (pem, pin, ssl.CERT_NONE),
            ]:
                policy = TLSPolicy.resolve(ca, fingerprint)
                self.assertEqual(policy.context("h2").verify_mode, mode)
            self.assertEqual(TLSPolicy.resolve(" \n", "").server_ca, "")
            with self.assertRaises(ssl.SSLError):
                TLSPolicy.resolve("invalid PEM", pin)


class TLSTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_https_proxy_verifies_proxy_before_auth_and_node_before_api_key(self):
        for transport in ("rest", "maintenance"):
            for credentials in ("", "user:password@"):
                for failure in (
                    None,
                    "trust",
                    "identity",
                    "expired",
                    "proxy-pin",
                    "pin",
                    "node-identity",
                    "node-expired",
                ):
                    with (
                        self.subTest(transport=transport, credentials=bool(credentials), failure=failure),
                        tempfile.TemporaryDirectory() as node_dir,
                        tempfile.TemporaryDirectory() as proxy_dir,
                    ):
                        node_ctx, _, pin = certificate(
                            node_dir,
                            identity="127.0.0.2" if failure == "node-identity" else "127.0.0.1",
                            dates="expired" if failure == "node-expired" else "valid",
                        )
                        proxy_ctx, proxy_pem, _ = certificate(
                            proxy_dir,
                            identity="127.0.0.2" if failure == "identity" else "127.0.0.1",
                            dates="expired" if failure == "expired" else "valid",
                        )
                        if failure == "proxy-pin":
                            # The destination pin must not authorize an untrusted proxy,
                            # even if it presents the pinned destination certificate.
                            proxy_ctx = node_ctx
                        received, connects = [], []

                        async def receive(request, received=received):
                            received.append(dict(request.headers))
                            return web.Response(body=b"ok")

                        app = web.Application()
                        app.router.add_get("/", receive)
                        runner = web.AppRunner(app)
                        await runner.setup()
                        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=node_ctx)
                        await site.start()
                        port = site._server.sockets[0].getsockname()[1]

                        async def tunnel(reader, writer, connects=connects, port=port):
                            upstream_writer = None
                            try:
                                connects.append(await reader.readuntil(b"\r\n\r\n"))
                                upstream_reader, upstream_writer = await asyncio.open_connection("127.0.0.1", port)
                                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                                await writer.drain()

                                async def copy(source, destination):
                                    try:
                                        while data := await source.read(65536):
                                            destination.write(data)
                                            await destination.drain()
                                    finally:
                                        destination.close()

                                await asyncio.gather(copy(reader, upstream_writer), copy(upstream_reader, writer))
                            except (ConnectionError, asyncio.IncompleteReadError):
                                pass
                            finally:
                                writer.close()
                                if upstream_writer is not None:
                                    upstream_writer.close()

                        proxy = await asyncio.start_server(tunnel, "127.0.0.1", 0, ssl=proxy_ctx)
                        proxy_port = proxy.sockets[0].getsockname()[1]
                        node = RestNode(
                            "127.0.0.1",
                            port,
                            port,
                            KEY,
                            certificate_sha256="00" * 32 if failure == "pin" else pin,
                            proxy=f"https://{credentials}127.0.0.1:{proxy_port}",
                        )
                        original = ssl.create_default_context

                        def proxy_trust(*args, original=original, failure=failure, proxy_pem=proxy_pem, **kwargs):
                            context = original(*args, **kwargs)
                            if failure != "trust":
                                context.load_verify_locations(cadata=proxy_pem)
                            return context

                        async def request(transport=transport, node=node):
                            if transport == "rest":
                                return await node._make_request("GET", "/", 3)
                            return await node._make_json_request("GET", "/")

                        try:
                            # Only the proxy is CA-trusted. The independent node
                            # certificate must still be accepted exclusively by pin.
                            with patch("ssl.create_default_context", side_effect=proxy_trust):
                                if failure:
                                    expected = {
                                        "pin": "fingerprint mismatch",
                                        "node-identity": "identity mismatch",
                                        "node-expired": "invalid dates",
                                    }.get(failure, "Certificate")
                                    with self.assertRaisesRegex(NodeAPIError, expected):
                                        await request()
                                    self.assertEqual(received, [])
                                    if failure in ("trust", "identity", "expired", "proxy-pin"):
                                        self.assertEqual(connects, [])
                                else:
                                    await request()
                                    await request()
                                    self.assertEqual(len(connects), 1)
                                    self.assertEqual(len(received), 2)
                                    self.assertTrue(all(headers.get("x-api-key") == KEY for headers in received))
                                    self.assertTrue(all("Proxy-Authorization" not in headers for headers in received))
                                if connects:
                                    self.assertNotIn(KEY.encode(), connects[0])
                                    self.assertEqual(b"Proxy-Authorization: Basic " in connects[0], bool(credentials))
                        finally:
                            await node._client.close()
                            await node._json_client.close()
                            proxy.close()
                            await proxy.wait_closed()
                            await runner.cleanup()

    async def asyncSetUp(self):
        # Rejected TLS handshakes normally reset the test server's accepted socket.
        loop = asyncio.get_running_loop()

        def expected_handshake_reset(loop, context):
            if isinstance(context.get("exception"), ConnectionResetError):
                return
            loop.default_exception_handler(context)

        loop.set_exception_handler(expected_handshake_reset)

    async def test_expiry_closes_active_stream(self):
        import aiohttp

        with tempfile.TemporaryDirectory() as directory:
            ctx, pem, pin = certificate(directory, dates="short")
            released = asyncio.Event()

            async def stream(request):
                response = web.StreamResponse()
                await response.prepare(request)
                await response.write(b"first line\n")
                await released.wait()
                return response

            app = web.Application()
            app.router.add_get("/", stream)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=ctx)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]
            node = RestNode("127.0.0.1", port, port, api_key=KEY, certificate_sha256=pin)
            try:
                response = await node._client.get("/")
                self.assertEqual(await response.content.readline(), b"first line\n")
                with self.assertRaises(aiohttp.ClientPayloadError):
                    await asyncio.wait_for(response.content.read(), timeout=4)
                response.close()
                expiry = x509.load_pem_x509_certificate(pem.encode()).not_valid_after_utc
                while datetime.now(UTC) < expiry:
                    await asyncio.sleep(0.01)
                with self.assertRaisesRegex(NodeAPIError, "invalid dates"):
                    await node._make_request("GET", "/", 3)
            finally:
                if "response" in locals():
                    response.close()
                released.set()
                await node._client.close()
                await node._json_client.close()
                await runner.cleanup()

    async def test_expiry_closes_active_grpc(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx, pem, pin = certificate(directory, dates="short")
            started, released = asyncio.Event(), asyncio.Event()

            class Receiver:
                async def receive(inner, stream):
                    await stream.recv_message()
                    started.set()
                    await released.wait()
                    await stream.send_message(service.BaseInfoResponse())

                def __mapping__(inner):
                    return {
                        "/service.NodeService/GetBaseInfo": Handler(
                            inner.receive, Cardinality.UNARY_UNARY, service.Empty, service.BaseInfoResponse
                        )
                    }

            server = Server([Receiver()])
            await server.start("127.0.0.1", 0, ssl=ctx)
            port = server._server.sockets[0].getsockname()[1]
            node = GrpcNode("127.0.0.1", port, port, api_key=KEY, certificate_sha256=pin)
            pending = asyncio.create_task(node._handle_grpc_request(node._client.GetBaseInfo, service.Empty()))
            try:
                await asyncio.wait_for(started.wait(), timeout=2)
                with self.assertRaises(NodeAPIError):
                    await asyncio.wait_for(pending, timeout=4)
                expiry = x509.load_pem_x509_certificate(pem.encode()).not_valid_after_utc
                while datetime.now(UTC) < expiry:
                    await asyncio.sleep(0.01)
                with self.assertRaisesRegex(NodeAPIError, "invalid dates"):
                    await node._handle_grpc_request(node._client.GetBaseInfo, service.Empty())
            finally:
                released.set()
                pending.cancel()
                node.channel.close()
                await node._json_client.close()
                server.close()
                await server.wait_closed()

    async def exercise(
        self,
        transport,
        *,
        failure=None,
        legacy=False,
        system=False,
        reconnect=False,
        proxy=False,
        protect_credentials=False,
        proxy_credentials=True,
    ):
        with tempfile.TemporaryDirectory() as directory:
            ctx, pem, pin = certificate(
                directory,
                identity="127.0.0.2" if failure == "identity" else "127.0.0.1",
                dates=failure if failure in ("expired", "future") else "valid",
                san=failure != "san",
            )
            received, connects = [], []
            if transport == "grpc":

                class Receiver:
                    async def receive(inner, stream):
                        received.append(dict(stream.metadata))
                        await stream.recv_message()
                        await stream.send_message(service.BaseInfoResponse())

                    def __mapping__(inner):
                        return {
                            "/service.NodeService/GetBaseInfo": Handler(
                                inner.receive, Cardinality.UNARY_UNARY, service.Empty, service.BaseInfoResponse
                            )
                        }

                server = Server([Receiver()])
                await server.start("127.0.0.1", 0, ssl=ctx)
                port = server._server.sockets[0].getsockname()[1]
                node_class = GrpcNode
            else:

                async def receive(request):
                    received.append(dict(request.headers))
                    await request.read()
                    if request.path == "/redirect":
                        return web.Response(status=302, headers={"Location": "/target"})
                    return web.Response(body=b"ok")

                app = web.Application()
                app.router.add_route("*", "/{path:.*}", receive)
                server = web.AppRunner(app)
                await server.setup()
                site = web.TCPSite(server, "127.0.0.1", 0, ssl_context=ctx)
                await site.start()
                port = site._server.sockets[0].getsockname()[1]
                node_class = RestNode
            proxy_server = None
            if proxy:

                async def tunnel(reader, writer):
                    try:
                        if str(proxy).startswith("socks5"):
                            greeting = await reader.readexactly(2)
                            await reader.readexactly(greeting[1])
                            writer.write(b"\x05\x00")
                            await writer.drain()
                            header = await reader.readexactly(4)
                            size = {1: 4, 4: 16}.get(header[3])
                            if size is None:
                                size = (await reader.readexactly(1))[0]
                            await reader.readexactly(size + 2)
                        elif str(proxy).startswith("socks4"):
                            header = await reader.readexactly(8)
                            await reader.readuntil(b"\x00")
                            if header[4:8] == b"\x00\x00\x00\x01":
                                await reader.readuntil(b"\x00")
                        else:
                            connects.append(await reader.readuntil(b"\r\n\r\n"))
                        upstream_reader, upstream_writer = await asyncio.open_connection("127.0.0.1", port)
                        if str(proxy).startswith("socks5"):
                            writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
                        elif str(proxy).startswith("socks4"):
                            writer.write(b"\x00\x5a\x00\x00\x7f\x00\x00\x01")
                        else:
                            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")

                        async def copy(source, destination):
                            try:
                                while data := await source.read(65536):
                                    destination.write(data)
                                    await destination.drain()
                            finally:
                                destination.close()

                        await asyncio.gather(copy(reader, upstream_writer), copy(upstream_reader, writer))
                    except (ConnectionError, asyncio.IncompleteReadError):
                        writer.close()

                proxy_server = await asyncio.start_server(tunnel, "127.0.0.1", 0, ssl=ctx if proxy == "https" else None)
            kwargs = {}
            if proxy_server:
                credentials = "user:password@" if protect_credentials and proxy_credentials else ""
                kwargs["proxy"] = f"{proxy}://{credentials}127.0.0.1:{proxy_server.sockets[0].getsockname()[1]}"
            ca = pem if legacy or failure == "pin" else None
            fingerprint = None if legacy or system or failure == "trust" else ("00" * 32 if failure == "pin" else pin)
            # System trust uses an isolated test trust store without modifying the host.
            original = ssl.create_default_context

            def trusted_context(*args, **kw):
                context = original(*args, **kw)
                context.load_verify_locations(cadata=pem)
                return context

            with patch(
                "ssl.create_default_context", side_effect=trusted_context if system or proxy == "https" else original
            ):
                node = node_class("127.0.0.1", port, port, KEY, server_ca=ca, certificate_sha256=fingerprint, **kwargs)

            if protect_credentials:
                for client in (node._client, node._json_client):
                    client._headers["aUtHoRiZaTiOn"] = "Bearer node-secret"

            async def request():
                # Sessions create their connectors lazily, including proxy trust.
                with patch(
                    "ssl.create_default_context",
                    side_effect=trusted_context if system or proxy == "https" else original,
                ):
                    if transport == "grpc":
                        return await node._handle_grpc_request(node._client.GetBaseInfo, service.Empty())
                    if transport == "maintenance":
                        return await node._make_json_request("GET", "/")
                    return await node._make_request("POST", "/", 3, service.Empty())

            try:
                if failure == "race":
                    connected, resume = asyncio.Event(), asyncio.Event()
                    original_connect = Channel._create_connection

                    async def delayed_connect(channel):
                        protocol = await original_connect(channel)
                        connected.set()
                        await resume.wait()
                        return protocol

                    with patch.object(Channel, "_create_connection", delayed_connect):
                        pending = asyncio.create_task(request())
                        try:
                            await asyncio.wait_for(connected.wait(), timeout=3)
                            await node.configure_tls()
                            resume.set()
                            with self.assertRaisesRegex(NodeAPIError, "configuration changed"):
                                await pending
                            self.assertEqual(received, [])
                        finally:
                            resume.set()
                            if not pending.done():
                                pending.cancel()
                                await asyncio.gather(pending, return_exceptions=True)
                elif failure:
                    expected = {
                        "pin": "fingerprint mismatch",
                        "expired": "invalid dates",
                        "future": "invalid dates",
                        "identity": "identity mismatch",
                        "san": "identity mismatch",
                        "trust": "CERTIFICATE_VERIFY_FAILED",
                    }[failure]
                    if system and failure == "identity":
                        expected = "mismatch"
                    with self.assertRaisesRegex(NodeAPIError, expected):
                        await request()
                    self.assertEqual(received, [])
                else:
                    await request()
                    if reconnect:
                        if transport == "grpc":
                            node.channel.close()
                        else:
                            await node._client.close()
                            await node._json_client.close()
                    await request()
                    self.assertEqual(len(received), 2)
                    self.assertTrue(
                        all(
                            any(k.lower() == "x-api-key" and v == KEY for k, v in headers.items())
                            for headers in received
                        )
                    )
                    if protect_credentials:
                        self.assertTrue(connects)
                        for connect in connects:
                            self.assertNotIn(b"x-api-key:", connect.lower())
                            self.assertNotIn(KEY.encode(), connect)
                            self.assertNotIn(b"\r\nauthorization:", connect.lower())
                            self.assertNotIn(b"node-secret", connect)
                            if proxy_credentials:
                                self.assertIn(b"Proxy-Authorization: Basic dXNlcjpwYXNzd29yZA==", connect)
                            else:
                                self.assertNotIn(b"proxy-authorization:", connect.lower())
                        for headers in received:
                            self.assertEqual(
                                {key.lower(): value for key, value in headers.items()}["authorization"],
                                "Bearer node-secret",
                            )
                    previous_policy = node._tls_policy
                    with self.assertRaises(NodeAPIError):
                        await node.configure_tls(certificate_sha256="malformed")
                    self.assertIs(node._tls_policy, previous_policy)
                    await node.configure_tls(server_ca=pem, certificate_sha256="00" * 32)
                    with self.assertRaisesRegex(NodeAPIError, "fingerprint mismatch"):
                        await request()
                    self.assertEqual(len(received), 2)
                    await node.configure_tls(server_ca=pem)
                    await request()
                    self.assertEqual(len(received), 3)
                    if transport != "grpc":
                        with self.assertRaises(NodeAPIError):
                            await node._make_json_request("GET", "/redirect")
                        self.assertEqual(len(received), 4)
            finally:
                if transport == "grpc":
                    node.channel.close()
                    server.close()
                    await server.wait_closed()
                else:
                    await node._client.close()
                    await server.cleanup()
                await node._json_client.close()
                if proxy_server:
                    proxy_server.close()
                    await proxy_server.wait_closed()

    async def test_policy_change_during_grpc_connect(self):
        await self.exercise("grpc", failure="race")

    async def test_pinned_failures_withhold_credentials(self):
        for transport in ("rest", "grpc", "maintenance"):
            for failure in ("pin", "expired", "future", "identity", "san"):
                with self.subTest(transport=transport, failure=failure):
                    await self.exercise(transport, failure=failure)

    async def test_success_reconnect_and_http_proxy(self):
        for transport in ("rest", "grpc", "maintenance"):
            for proxy in (False, "http", "https", "socks4", "socks4a", "socks5", "socks5h"):
                if proxy == "https" and transport == "grpc":
                    continue
                with self.subTest(transport=transport, proxy=proxy):
                    await self.exercise(transport, reconnect=True, proxy=proxy)
                    if proxy:
                        await self.exercise(transport, proxy=proxy, failure="pin")

    async def test_connect_filters_node_credentials_in_all_tls_modes(self):
        for transport in ("rest", "maintenance"):
            for proxy in ("http", "https"):
                for mode in ("pinned", "pem", "system"):
                    for credentials in (False, True):
                        with self.subTest(transport=transport, proxy=proxy, mode=mode, credentials=credentials):
                            await self.exercise(
                                transport,
                                proxy=proxy,
                                legacy=mode == "pem",
                                system=mode == "system",
                                protect_credentials=True,
                                proxy_credentials=credentials,
                            )

    async def test_pem_and_system_trust(self):
        for transport in ("rest", "grpc", "maintenance"):
            for legacy, system in ((True, False), (False, True)):
                with self.subTest(transport=transport, legacy=legacy):
                    await self.exercise(transport, legacy=legacy, system=system)
            await self.exercise(transport, system=True, failure="identity")
            await self.exercise(transport, system=False, failure="trust", legacy=False)


@unittest.skipIf(uvloop is None, "uvloop is unavailable on this platform")
class UVLoopTLSTransportTests(TLSTransportTests):
    @staticmethod
    def loop_factory():
        return uvloop.new_event_loop()

    def _setupAsyncioRunner(self):
        # IsolatedAsyncioTestCase gained loop_factory in Python 3.13.
        self._asyncioRunner = asyncio.Runner(debug=True, loop_factory=self.loop_factory)
