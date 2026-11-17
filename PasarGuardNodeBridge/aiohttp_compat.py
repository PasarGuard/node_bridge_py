import json
import ssl
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from types import TracebackType
from typing import Any, cast

import aiohttp

from PasarGuardNodeBridge.tls import NodeTCPConnector, PinnedTCPConnector, TLSPolicy, TLSStatus


def make_timeout(timeout: float | None) -> aiohttp.ClientTimeout:
    """Apply the same timeout value across aiohttp's available request phases."""
    if timeout is None:
        return aiohttp.ClientTimeout(total=None)
    return aiohttp.ClientTimeout(total=timeout, connect=timeout, sock_connect=timeout, sock_read=timeout)


@dataclass(slots=True)
class BufferedResponse:
    status_code: int
    content: bytes
    headers: Mapping[str, str]
    url: str
    reason: str | None = None
    charset: str | None = None

    @property
    def text(self) -> str:
        return self.content.decode(self.charset or "utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        if 300 <= self.status_code:
            raise BufferedStatusError(self)


class BufferedStatusError(Exception):
    def __init__(self, response: BufferedResponse) -> None:
        self.response = response
        super().__init__(f"{response.status_code} {response.reason or ''}".strip())


async def buffer_response(response: aiohttp.ClientResponse) -> BufferedResponse:
    body = await response.read()
    return BufferedResponse(
        status_code=response.status,
        content=body,
        headers=response.headers,
        url=str(response.url),
        reason=response.reason,
        charset=response.charset,
    )


class LazyClientSession:
    def __init__(
        self,
        *,
        ssl_context: ssl.SSLContext,
        headers: dict[str, str],
        base_url: str,
        timeout: aiohttp.ClientTimeout,
        connector_factory: Callable[..., aiohttp.BaseConnector] | None = None,
        proxy: str | None = None,
        proxy_auth: aiohttp.BasicAuth | None = None,
        tls_policy: TLSPolicy | None = None,
        tls_status: TLSStatus | None = None,
    ) -> None:
        self.tls_status = tls_status
        self._tls_policy = tls_policy
        self._ssl_context = ssl_context
        self._headers = headers
        self._base_url = base_url
        self._timeout = timeout
        self._connector_factory = connector_factory
        self._proxy = proxy
        self._proxy_auth = proxy_auth
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = None
            if self._tls_policy is not None:
                if self._connector_factory is not None:
                    # The factory carries the parsed SOCKS URL; build the guarded variant.
                    connector = self._connector_factory(
                        self._ssl_context, tls_policy=self._tls_policy, tls_status=self.tls_status
                    )
                else:
                    connector = PinnedTCPConnector(
                        ssl=self._ssl_context, tls_policy=self._tls_policy, tls_status=self.tls_status
                    )
            elif self._connector_factory is not None:
                connector = self._connector_factory(self._ssl_context)
            else:
                connector = NodeTCPConnector(ssl=self._ssl_context)
            self._session = aiohttp.ClientSession(
                connector=connector,
                headers=self._headers,
                base_url=self._base_url,
                timeout=self._timeout,
                proxy=self._proxy,
                proxy_auth=self._proxy_auth,
            )
        return self._session

    def request(self, *args: Any, **kwargs: Any) -> "_LazyRequestContext":
        kwargs["allow_redirects"] = False
        return _LazyRequestContext(self, args, kwargs)

    async def get(self, *args: Any, **kwargs: Any) -> aiohttp.ClientResponse:
        session = await self._get_session()
        kwargs["allow_redirects"] = False
        return await session.get(*args, **kwargs)

    def replace_tls(
        self, ssl_context: ssl.SSLContext, tls_policy: TLSPolicy, tls_status: TLSStatus | None = None
    ) -> aiohttp.ClientSession | None:
        self._ssl_context = ssl_context
        self._tls_policy = tls_policy
        self.tls_status = tls_status
        old, self._session = self._session, None
        return old

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()


class _LazyRequestContext:
    def __init__(self, client: LazyClientSession, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        self._client = client
        self._args = args
        self._kwargs = kwargs
        self._context_manager: AbstractAsyncContextManager[aiohttp.ClientResponse] | None = None

    async def __aenter__(self) -> aiohttp.ClientResponse:
        session = await self._client._get_session()
        self._context_manager = session.request(*self._args, **self._kwargs)
        return await self._context_manager.__aenter__()

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: TracebackType | None
    ) -> bool | None:
        context_manager = cast(AbstractAsyncContextManager[aiohttp.ClientResponse], self._context_manager)
        return await context_manager.__aexit__(exc_type, exc_val, exc_tb)
