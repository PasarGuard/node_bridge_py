"""Local socket tests: real framing, server failures and queue recovery on both transports."""

import asyncio
import logging
import unittest
from unittest.mock import patch

from aiohttp import web
from grpclib.client import Channel
from grpclib.const import Cardinality, Handler, Status
from grpclib.exceptions import GRPCError
from grpclib.server import Server

from PasarGuardNodeBridge.aiohttp_compat import LazyClientSession, make_timeout
from PasarGuardNodeBridge.common import service_grpc
from PasarGuardNodeBridge.common import service_pb2 as service
from PasarGuardNodeBridge.grpclib import Node as GrpcNode
from PasarGuardNodeBridge.rest import Node as RestNode
from PasarGuardNodeBridge.storage import InMemoryUserSyncStore


class SyncTransportTests(unittest.IsolatedAsyncioTestCase):
    def make_node(self, node_class, port, **kwargs):
        with patch("ssl.SSLContext.load_verify_locations"):
            return node_class(
                address="127.0.0.1",
                port=port,
                api_port=1,
                server_ca="test",
                api_key="00000000-0000-0000-0000-000000000000",
                node_id="transport",
                user_sync_store=InMemoryUserSyncStore(),
                sync_poll_interval=0.01,
                sync_chunk_size=35,
                logger=logging.getLogger("transport-test"),
                **kwargs,
            )

    async def drain(self, node, emails, inbounds=()):
        users = [service.User(email=email, inbounds=inbounds) for email in emails]
        await node._user_sync_store.enqueue_users(node.node_id, users)
        await node.connect("0.2.0", "1.0.0")
        async with asyncio.timeout(10):
            while await node._user_sync_store.has_pending(node.node_id):
                await asyncio.sleep(0.01)

    async def test_grpc_streams_bounded_batches_and_retries_server_failure(self):
        batches, delivered, snapshots = [], [], []

        class Receiver:
            async def receive(inner, stream):
                chunks = [chunk async for chunk in stream]
                self.assertEqual([chunk.index for chunk in chunks], list(range(len(chunks))))
                self.assertEqual([chunk.last for chunk in chunks], [False] * (len(chunks) - 1) + [True])
                self.assertTrue(all(len(chunk.users) <= 35 for chunk in chunks))
                users = [user.email for chunk in chunks for user in chunk.users]
                batches.append(users)
                if len(batches) == 1:
                    raise GRPCError(Status.UNAVAILABLE, "temporary receiver failure")
                delivered.extend(users)
                await stream.send_message(service.Empty())

            async def snapshot(inner, stream):
                request = await stream.recv_message()
                snapshots.append([user.email for user in request.users])
                await stream.send_message(service.Empty())

            def __mapping__(inner):
                return {
                    "/service.NodeService/SyncUsersChunked": Handler(
                        inner.receive, Cardinality.STREAM_UNARY, service.UsersChunk, service.Empty
                    ),
                    "/service.NodeService/SyncUsers": Handler(
                        inner.snapshot, Cardinality.UNARY_UNARY, service.Users, service.Empty
                    ),
                }

        server = Server([Receiver()])
        await server.start("127.0.0.1", 0)
        port = server._server.sockets[0].getsockname()[1]
        node = self.make_node(GrpcNode, port)
        node.channel.close()
        node.channel = Channel("127.0.0.1", port, ssl=False)
        node._client = service_grpc.NodeServiceStub(node.channel)
        try:
            emails = [f"user-{i}" for i in range(1200)]
            await self.drain(node, emails)
            self.assertEqual(len(batches), 13)
            self.assertTrue(all(1 <= len(batch) <= 100 for batch in batches))
            self.assertCountEqual(delivered, emails)
            await node.sync_users([service.User(email="full")])
            self.assertEqual(snapshots, [["full"]])
            self.assertEqual(await node.sync_users_chunked([]), [])
        finally:
            await node.disconnect()
            node.channel.close()
            await node._json_client.close()
            server.close()
            await server.wait_closed()

    async def test_rest_streams_bounded_batches_and_retries_server_failure(self):
        batches, delivered, snapshots = [], [], []

        async def receive(request):
            body = await request.read()
            chunks = []
            offset = 0
            while offset < len(body):
                length = shift = 0
                while True:
                    byte = body[offset]
                    offset += 1
                    length |= (byte & 127) << shift
                    if byte < 128:
                        break
                    shift += 7
                chunks.append(service.UsersChunk.FromString(body[offset : offset + length]))
                offset += length
            self.assertEqual([chunk.index for chunk in chunks], list(range(len(chunks))))
            self.assertEqual([chunk.last for chunk in chunks], [False] * (len(chunks) - 1) + [True])
            self.assertTrue(all(len(chunk.users) <= 35 for chunk in chunks))
            users = [user.email for chunk in chunks for user in chunk.users]
            batches.append(users)
            if len(batches) == 1:
                return web.Response(status=503)
            delivered.extend(users)
            return web.Response(body=service.Empty().SerializeToString())

        async def snapshot(request):
            users = service.Users.FromString(await request.read())
            snapshots.append([user.email for user in users.users])
            return web.Response(body=service.Empty().SerializeToString())

        app = web.Application()
        app.router.add_put("/users/sync/chunked", receive)
        app.router.add_put("/users/sync", snapshot)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        node = self.make_node(RestNode, port)
        await node._client.close()
        node._client = LazyClientSession(
            ssl_context=None,
            headers={"Content-Type": "application/x-protobuf"},
            base_url=f"http://127.0.0.1:{port}/",
            timeout=make_timeout(None),
        )
        try:
            emails = [f"user-{i}" for i in range(1200)]
            await self.drain(node, emails)
            self.assertEqual(len(batches), 13)
            self.assertTrue(all(1 <= len(batch) <= 100 for batch in batches))
            self.assertCountEqual(delivered, emails)
            await node.sync_users([service.User(email="full")])
            self.assertEqual(snapshots, [["full"]])
            self.assertEqual(await node.sync_users_chunked([]), [])
        finally:
            await node.disconnect()
            await node._client.close()
            await node._json_client.close()
            await runner.cleanup()

    async def start_grpc(self, apply):
        """A SyncUsersChunked server answering like the node: INTERNAL when apply() refuses the batch."""

        class Receiver:
            async def receive(inner, stream):
                emails = [user.email async for chunk in stream for user in chunk.users]
                error = apply(emails)
                if error is not None:
                    raise error
                await stream.send_message(service.Empty())

            def __mapping__(inner):
                return {
                    "/service.NodeService/SyncUsersChunked": Handler(
                        inner.receive, Cardinality.STREAM_UNARY, service.UsersChunk, service.Empty
                    )
                }

        server = Server([Receiver()])
        await server.start("127.0.0.1", 0)
        port = server._server.sockets[0].getsockname()[1]
        node = self.make_node(GrpcNode, port)
        node.channel.close()
        node.channel = Channel("127.0.0.1", port, ssl=False)
        node._client = service_grpc.NodeServiceStub(node.channel)

        async def cleanup():
            await node.disconnect()
            node.channel.close()
            await node._json_client.close()
            server.close()
            await server.wait_closed()

        self.addAsyncCleanup(cleanup)
        return node

    async def start_rest(self, apply):
        """A /users/sync/chunked server answering like the node: an error status when apply() refuses."""

        async def receive(request):
            body = await request.read()
            emails, offset = [], 0
            while offset < len(body):
                length = shift = 0
                while True:
                    byte = body[offset]
                    offset += 1
                    length |= (byte & 127) << shift
                    if byte < 128:
                        break
                    shift += 7
                emails += [user.email for user in service.UsersChunk.FromString(body[offset : offset + length]).users]
                offset += length
            error = apply(emails)
            if error is not None:
                return error
            return web.Response(body=service.Empty().SerializeToString())

        app = web.Application()
        app.router.add_put("/users/sync/chunked", receive)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        node = self.make_node(RestNode, port)
        await node._client.close()
        node._client = LazyClientSession(
            ssl_context=None,
            headers={"Content-Type": "application/x-protobuf"},
            base_url=f"http://127.0.0.1:{port}/",
            timeout=make_timeout(None),
        )

        async def cleanup():
            await node.disconnect()
            await node._client.close()
            await node._json_client.close()
            await runner.cleanup()

        self.addAsyncCleanup(cleanup)
        return node

    async def test_user_the_node_rejects_does_not_hold_back_later_users(self):
        # The node fails the whole batch when its backend cannot apply one user in it, e.g. an
        # SS2022 inbound Xray started without clients ("proxy is not a UserManager").
        reason = "failed to update users:\napp/proxyman/command: proxy is not a UserManager"
        rejections = {
            "grpc": lambda: GRPCError(Status.INTERNAL, f"failed to update users: {reason}"),
            "rest": lambda: web.Response(status=500, text=reason + "\n"),
        }
        for transport, start in (("grpc", self.start_grpc), ("rest", self.start_rest)):
            with self.subTest(transport=transport):
                batches, delivered = [], []

                def apply(emails):
                    batches.append(emails)
                    if "bad" in emails:
                        return rejections[transport]()
                    delivered.extend(emails)
                    return None

                node = await start(apply)
                emails = [f"user-{i}" for i in range(150)]
                await self.drain(node, ["bad", *emails], inbounds=["ss2022"])
                # The queue drained with the rejected user held aside, not at its head.
                self.assertCountEqual(delivered, emails)
                self.assertEqual(list(node._sync_retries), ["bad"])
                self.assertLess(len(batches), 30)

    async def test_only_backend_rejections_are_split(self):
        grpc_cases = [
            (GRPCError(Status.INTERNAL, "failed to update users: failed to update users:\nboom"), True),
            (GRPCError(Status.INTERNAL, "failed to receive chunk: stream reset"), False),
            (GRPCError(Status.UNAVAILABLE, "core is not started yet"), False),
        ]
        rest_cases = [
            (web.Response(status=500, text="failed to update users:\nboom\n"), True),
            (web.Response(status=500, text="wireguard public key k is already assigned to user 3\n"), True),
            (web.Response(status=500, text="backend not initialized\n"), False),
            (web.Response(status=503, text="core is not started yet\n"), False),
        ]
        for start, cases in ((self.start_grpc, grpc_cases), (self.start_rest, rest_cases)):
            answers = []
            node = await start(lambda emails: answers.pop(0))
            for answer, rejected in cases:
                with self.subTest(transport=node.__module__, answer=str(answer)):
                    answers.append(answer)
                    users = [service.User(email="a"), service.User(email="b")]
                    failed = await node.sync_users_chunked(users)
                    self.assertEqual(failed, users)
                    self.assertIs(failed.rejected, rejected)

        for node_class in (GrpcNode, RestNode):
            with self.subTest(transport=node_class.__module__, answer="connection refused"):
                node = self.make_node(node_class, 1)
                try:
                    failed = await node.sync_users_chunked([service.User(email="a")])
                    self.assertEqual(len(failed), 1)
                    self.assertFalse(failed.rejected)
                finally:
                    await node._json_client.close()
                    if isinstance(node, GrpcNode):
                        node.channel.close()
                    else:
                        await node._client.close()

    async def test_transport_lock_wait_cannot_outlive_claim_lease(self):
        for node_class in (GrpcNode, RestNode):
            with self.subTest(transport=node_class.__module__):
                node = self.make_node(node_class, 1, sync_lease_seconds=0.2)
                await node._node_lock.acquire()
                try:
                    await node.update_user(service.User(email="a"))
                    await node.connect("0.2.0", "1.0.0")
                    async with asyncio.timeout(0.8):
                        while not node._user_sync_failure_count:
                            await asyncio.sleep(0.01)
                    captured, active = await node._user_sync_store.capture_queued(node.node_id)
                    self.assertEqual(active, 0)
                    self.assertEqual(list(captured), ["a"])
                finally:
                    await node.disconnect()
                    node._node_lock.release()
                    await node._json_client.close()
                    if isinstance(node, GrpcNode):
                        node.channel.close()
                    else:
                        await node._client.close()
