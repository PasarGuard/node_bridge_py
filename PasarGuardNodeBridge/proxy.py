from collections.abc import Callable
from dataclasses import dataclass
from ssl import SSLContext
from urllib.parse import urlsplit

import aiohttp
from aiohttp_socks import ProxyConnector

from PasarGuardNodeBridge.tls import PinnedProxyConnector, TLSPolicy, TLSStatus

SUPPORTED_PROXY_SCHEMES = {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}
GRPC_PROXY_SCHEMES = {"http", "socks4", "socks4a", "socks5", "socks5h"}
SOCKS_PROXY_SCHEMES = {"socks4", "socks4a", "socks5", "socks5h"}


@dataclass(frozen=True, slots=True)
class ProxyConfig:
    url: str
    scheme: str
    grpc_supported: bool
    aiohttp_proxy_url: str | None
    aiohttp_proxy_auth: aiohttp.BasicAuth | None
    aiohttp_connector_factory: Callable[..., aiohttp.BaseConnector] | None
    rdns: bool | None = None


def parse_proxy_url(proxy: str | None) -> ProxyConfig | None:
    if proxy is None:
        return None

    proxy = proxy.strip()
    if not proxy:
        return None

    parsed = urlsplit(proxy)
    scheme = parsed.scheme.lower()
    if scheme not in SUPPORTED_PROXY_SCHEMES:
        raise ValueError("Unsupported proxy scheme. Expected one of: http, https, socks4, socks4a, socks5, socks5h")
    if not parsed.hostname or parsed.port is None:
        raise ValueError("Proxy URL must include host and port")

    if scheme in SOCKS_PROXY_SCHEMES:
        rdns = scheme in {"socks4a", "socks5h"}
        canonical_url = parsed._replace(scheme="socks4" if scheme.startswith("socks4") else "socks5").geturl()

        def connector_factory(
            ssl_context: SSLContext, *, tls_policy: TLSPolicy | None = None, tls_status: TLSStatus | None = None
        ) -> aiohttp.BaseConnector:
            if tls_policy is not None:
                return PinnedProxyConnector.from_url(
                    canonical_url, ssl=ssl_context, tls_policy=tls_policy, tls_status=tls_status, rdns=rdns
                )
            return ProxyConnector.from_url(canonical_url, ssl=ssl_context, rdns=rdns)

        return ProxyConfig(
            url=canonical_url,
            rdns=rdns,
            scheme=scheme,
            grpc_supported=scheme in GRPC_PROXY_SCHEMES,
            aiohttp_proxy_url=None,
            aiohttp_proxy_auth=None,
            aiohttp_connector_factory=connector_factory,
        )

    proxy_auth = None
    if parsed.username is not None:
        proxy_auth = aiohttp.BasicAuth(parsed.username, parsed.password or "")

    return ProxyConfig(
        url=proxy,
        scheme=scheme,
        grpc_supported=scheme in GRPC_PROXY_SCHEMES,
        aiohttp_proxy_url=proxy,
        aiohttp_proxy_auth=proxy_auth,
        aiohttp_connector_factory=None,
    )
