from grpclib.const import Status
from http import HTTPStatus
from ipaddress import ip_address

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


def normalize_host(address: str) -> str:
    """
    Normalize a host/address string for socket connections by stripping
    whitespace, schemes, paths, surrounding brackets, and ports.
    """
    raw = address.strip()
    if not raw:
        return ""

    # Drop scheme if provided
    if "://" in raw:
        raw = raw.split("://", 1)[1]

    # Normalize slashes and remove path/query/fragment
    raw = raw.lstrip("/")
    for sep in ("#", "?"):
        if sep in raw:
            raw = raw.split(sep, 1)[0]
    if "/" in raw:
        raw = raw.split("/", 1)[0]

    # Drop optional credentials (user:pass@host)
    if "@" in raw:
        raw = raw.rsplit("@", 1)[1]

    host = raw

    # Bracketed IPv6 literal
    if host.startswith("["):
        end = host.find("]")
        if end != -1:
            host = host[1:end]
        else:
            host = host[1:]

    # Pure IP literal
    try:
        parsed_ip = ip_address(host)
        return parsed_ip.compressed
    except ValueError:
        pass

    # IPv6 literal with inline port but without brackets
    if host.count(":") >= 2:
        maybe_host, maybe_port = host.rsplit(":", 1)
        if maybe_port.isdigit():
            try:
                parsed_ip = ip_address(maybe_host)
                return parsed_ip.compressed
            except ValueError:
                pass
        # Looks IPv6-like; return as-is without trying to strip further
        return host

    # Hostname/IPv4 with port (single colon)
    if ":" in host:
        maybe_host, maybe_port = host.rsplit(":", 1)
        if maybe_port.isdigit():
            host = maybe_host

    return host


def format_host_for_url(address: str) -> str:
    """
    Format a host/address for inclusion in an HTTP URL.

    - IPv6 addresses are wrapped in brackets.
    - Hosts with multiple colons (IPv6-like) are bracketed to avoid port confusion.
    - IPv4/hostnames are returned unchanged (after normalization).
    """
    host = normalize_host(address)
    if not host:
        return ""
    try:
        parsed = ip_address(host)
        if parsed.version == 6:
            return f"[{parsed.compressed}]"
        return parsed.compressed
    except ValueError:
        # If it looks like an IPv6 literal (multiple colons) but ipaddress failed,
        # still bracket it so URL parsing won't treat parts as a port.
        if host.count(":") >= 2:
            return f"[{host}]"
        return host
