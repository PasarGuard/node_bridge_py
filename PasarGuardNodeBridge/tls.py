"""Shared, fail-closed node TLS policy and verification on application connections."""

import asyncio
import hashlib
import hmac
import re
import ssl
import weakref
from dataclasses import dataclass
from datetime import UTC, datetime
from ipaddress import ip_address
from typing import Any, Self

import aiohttp
from aiohttp.client_proto import ResponseHandler
from aiohttp.client_reqrep import ConnectionKey
from aiohttp.connector import Connection
from aiohttp.tracing import Trace
from aiohttp_socks import ProxyConnector
from cryptography import x509


class TLSVerificationError(aiohttp.ClientConnectionError):
    """A pinned peer failed certificate verification."""


def verification_error_detail(error: BaseException) -> str:
    certificate_error = getattr(error, "certificate_error", error)
    if isinstance(certificate_error, ssl.SSLCertVerificationError):
        code = certificate_error.verify_code
        if code in (9, 10):
            reason = "Certificate invalid dates"
        elif code in (62, 64):
            reason = "Certificate identity mismatch"
        elif code in (18, 19, 20, 21):
            reason = "Certificate untrusted issuer"
        else:
            reason = "Certificate verification failed"
        return f"{reason}: {certificate_error}"
    return str(error)


def normalize_fingerprint(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not re.fullmatch(
        r"(?:[0-9a-fA-F]{64}|[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){31})", value
    ):
        raise ValueError("certificate_sha256 must contain 64 hexadecimal characters or 32 colon-separated bytes")
    return value.replace(":", "").lower()


def _dns_matches(pattern: str, host: str) -> bool:
    pattern = pattern.lower()
    host = host.encode("idna").decode("ascii").lower()
    if "*" not in pattern:
        return pattern == host
    # Only a complete leftmost label may be a wildcard, matching exactly one label.
    labels = pattern.split(".")
    return len(labels) >= 3 and labels[0] == "*" and "*" not in pattern[2:] and host.split(".")[1:] == labels[1:]


@dataclass(frozen=True)
class TLSPolicy:
    server_ca: str
    certificate_sha256: str | None

    @classmethod
    def resolve(cls, server_ca: str | None, certificate_sha256: str | None) -> Self:
        pem = "" if server_ca is None else server_ca
        if not isinstance(pem, str):
            raise TypeError("server_ca must be PEM text")
        pem = pem if pem.strip() else ""
        pin = normalize_fingerprint(certificate_sha256)
        # Validate even inactive PEM. Preserve the existing default trust store.
        if pem:
            ctx = ssl.create_default_context()
            ctx.load_verify_locations(cadata=pem)
        return cls(pem, pin)

    def context(self, alpn: str) -> ssl.SSLContext:
        if self.certificate_sha256:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        else:
            ctx = ssl.create_default_context()
            if self.server_ca:
                ctx.load_verify_locations(cadata=self.server_ca)
        ctx.set_alpn_protocols([alpn])
        return ctx

    def verify(self, transport: asyncio.Transport, host: str) -> datetime:
        """Verify the actual TLS peer, returning its expiry for connection retirement."""
        peer = transport.get_extra_info("ssl_object")
        if peer is None:
            raise TLSVerificationError("TLS is required")
        der = peer.getpeercert(binary_form=True)
        if not der or not hmac.compare_digest(hashlib.sha256(der).hexdigest(), self.certificate_sha256):
            raise TLSVerificationError("Certificate fingerprint mismatch")
        cert = x509.load_der_x509_certificate(der)
        now = datetime.now(UTC)
        if not cert.not_valid_before_utc <= now < cert.not_valid_after_utc:
            raise TLSVerificationError("Certificate invalid dates: expired or not yet valid")
        try:
            sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        except x509.ExtensionNotFound:
            raise TLSVerificationError("Certificate identity mismatch: missing SAN") from None
        try:
            address = ip_address(host)
        except ValueError:
            matched = any(_dns_matches(name, host) for name in sans.get_values_for_type(x509.DNSName))
        else:
            matched = address in sans.get_values_for_type(x509.IPAddress)
        if not matched:
            raise TLSVerificationError(f"Certificate identity mismatch: SAN does not match {host}")
        return cert.not_valid_after_utc

    def guard(self, transport: asyncio.Transport, host: str) -> None:
        if not self.certificate_sha256:
            return
        try:
            expiry = self.verify(transport, host)
        except BaseException:
            transport.abort()
            raise
        # Abort active streams as well as idle pooled connections at certificate expiry.
        loop = asyncio.get_running_loop()
        delay = max(0, (expiry - datetime.now(UTC)).total_seconds())
        reference = weakref.ref(transport)

        def expire() -> None:
            current = reference()
            if current is not None:
                current.abort()

        timer = loop.call_later(delay, expire)
        weakref.finalize(transport, timer.cancel)


class _PinnedConnectorMixin:
    def __init__(self, *args: Any, tls_policy: TLSPolicy, **kwargs: Any) -> None:
        self._tls_policy = tls_policy
        super().__init__(*args, **kwargs)

    async def _create_connection(
        self, req: aiohttp.ClientRequest, traces: list[Trace], timeout: aiohttp.ClientTimeout
    ) -> ResponseHandler:
        protocol = await super()._create_connection(req, traces, timeout)
        self._tls_policy.guard(protocol.transport, req.url.raw_host)
        return protocol

    async def _get(self, key: ConnectionKey, traces: list[Trace]) -> Connection | None:
        connection = await super()._get(key, traces)
        if connection is not None:
            try:
                self._tls_policy.verify(connection.transport, key.host)
            except BaseException:
                connection.close()
                raise
        return connection


class NodeTCPConnector(aiohttp.TCPConnector):
    """Keep destination credentials out of HTTP proxy CONNECT requests."""

    async def _create_proxy_connection(
        self, req: aiohttp.ClientRequest, traces: list[Trace], timeout: aiohttp.ClientTimeout
    ) -> tuple[asyncio.BaseTransport, ResponseHandler]:
        # aiohttp may merge session defaults into proxy_headers. Node credentials
        # belong only inside the verified destination tunnel.
        original_headers = req.proxy_headers
        if original_headers is not None:
            req.proxy_headers = original_headers.copy()
            req.proxy_headers.popall("x-api-key", None)
            req.proxy_headers.popall("Authorization", None)
        try:
            return await super()._create_proxy_connection(req, traces, timeout)
        finally:
            req.proxy_headers = original_headers


class PinnedTCPConnector(_PinnedConnectorMixin, NodeTCPConnector):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._proxy_ssl_context = ssl.create_default_context()
        self._proxy_ssl_context.set_alpn_protocols(["http/1.1"])

    async def _wrap_create_connection(
        self,
        *args: Any,
        req: aiohttp.ClientRequest,
        client_error: type[Exception] = aiohttp.ClientConnectorError,
        **kwargs: Any,
    ) -> tuple[asyncio.Transport, ResponseHandler]:
        # aiohttp marks the outer proxy connection with this error class. Verify
        # its CA and hostname before CONNECT can send proxy credentials; the
        # inner TLS connection keeps the node context and certificate pin.
        if client_error is aiohttp.ClientProxyConnectionError and kwargs.get("ssl") is not None:
            kwargs["ssl"] = self._proxy_ssl_context
        return await super()._wrap_create_connection(*args, req=req, client_error=client_error, **kwargs)


class PinnedProxyConnector(_PinnedConnectorMixin, ProxyConnector):
    pass
