from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit

import aiohttp
from aiohttp_socks import ProxyConnector

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
    aiohttp_connector_factory: Callable[[object], aiohttp.BaseConnector] | None


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

        def connector_factory(ssl_context) -> aiohttp.BaseConnector:
            return ProxyConnector.from_url(proxy, ssl=ssl_context)

        return ProxyConfig(
            url=proxy,
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
