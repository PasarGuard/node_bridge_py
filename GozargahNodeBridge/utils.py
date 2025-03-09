import tempfile

from GozargahNodeBridge.common.service_pb2 import User, Proxy, Vmess, Vless, Trojan, Shadowsocks


def string_to_temp_file(content: str):
    with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
        f.write(content)
        return f.name


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
