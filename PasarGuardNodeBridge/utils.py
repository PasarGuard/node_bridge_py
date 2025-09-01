from grpclib.const import Status
from http import HTTPStatus

from PasarGuardNodeBridge.common.service_pb2 import User, Proxy, Vmess, Vless, Trojan, Shadowsocks


def create_user(email: str, proxies: Proxy, inbounds: list[str]) -> User:
    return User(email=email, proxies=proxies, inbounds=inbounds)


def create_proxy(
    vmess_id: str | None = None,
    vless_id: str | None = None,
    vless_flow: str | None = None,
    trojan_password: str | None = None,
    shadowsocks_password: str | None = None,
    shadowsocks_method: str | None = None,
) -> Proxy:
    return Proxy(
        vmess=Vmess(id=vmess_id),
        vless=Vless(id=vless_id, flow=vless_flow),
        trojan=Trojan(password=trojan_password),
        shadowsocks=Shadowsocks(password=shadowsocks_password, method=shadowsocks_method),
    )


def grpc_to_http_status(grpc_status: Status) -> int:
    """Map gRPC status codes to HTTP status codes."""
    mapping = {
        Status.OK: HTTPStatus.OK.value,
        Status.CANCELLED: 499,
        Status.UNKNOWN: HTTPStatus.INTERNAL_SERVER_ERROR.value,
        Status.INVALID_ARGUMENT: HTTPStatus.BAD_REQUEST.value,
        Status.DEADLINE_EXCEEDED: HTTPStatus.GATEWAY_TIMEOUT.value,
        Status.NOT_FOUND: HTTPStatus.NOT_FOUND.value,
        Status.ALREADY_EXISTS: HTTPStatus.CONFLICT.value,
        Status.PERMISSION_DENIED: HTTPStatus.FORBIDDEN.value,
        Status.UNAUTHENTICATED: HTTPStatus.UNAUTHORIZED.value,
        Status.RESOURCE_EXHAUSTED: HTTPStatus.TOO_MANY_REQUESTS.value,
        Status.FAILED_PRECONDITION: HTTPStatus.PRECONDITION_FAILED.value,
        Status.ABORTED: HTTPStatus.CONFLICT.value,
        Status.OUT_OF_RANGE: HTTPStatus.BAD_REQUEST.value,
        Status.UNIMPLEMENTED: HTTPStatus.NOT_IMPLEMENTED.value,
        Status.INTERNAL: HTTPStatus.INTERNAL_SERVER_ERROR.value,
        Status.UNAVAILABLE: HTTPStatus.SERVICE_UNAVAILABLE.value,
        Status.DATA_LOSS: HTTPStatus.INTERNAL_SERVER_ERROR.value,
    }
    return mapping.get(grpc_status, HTTPStatus.INTERNAL_SERVER_ERROR.value)
