import inspect
import unittest

from PasarGuardNodeBridge.common import service_pb2 as service
from PasarGuardNodeBridge.utils import create_proxy

MTPROTO_SECRET = "0123456789abcdef0123456789abcdef"


def legacy_proxy(
    vmess_id=None,
    vless_id=None,
    vless_flow=None,
    trojan_password=None,
    shadowsocks_password=None,
    shadowsocks_method=None,
    wireguard_public_key=None,
    wireguard_peer_ips=None,
    hysteria_auth=None,
    masque_pass=None,
):
    """The Proxy that create_proxy built before MTProto support, field by field."""
    return service.Proxy(
        vmess=service.Vmess(id=vmess_id),
        vless=service.Vless(id=vless_id, flow=vless_flow),
        trojan=service.Trojan(password=trojan_password),
        shadowsocks=service.Shadowsocks(password=shadowsocks_password, method=shadowsocks_method),
        wireguard=service.Wireguard(public_key=wireguard_public_key, peer_ips=wireguard_peer_ips or []),
        hysteria=service.Hysteria(auth=hysteria_auth),
        masque=service.Masque(**{"pass": masque_pass}),
    )


FULL_LEGACY_ARGS = {
    "vmess_id": "0d59268a-9847-4218-ae09-65308eb52e08",
    "vless_id": "1e6a379b-a958-4329-bf1a-76419fc63f19",
    "vless_flow": "xtls-rprx-vision",
    "trojan_password": "trojan-pass",
    "shadowsocks_password": "ss-pass",
    "shadowsocks_method": "chacha20-ietf-poly1305",
    "wireguard_public_key": "wg-public-key",
    "wireguard_peer_ips": ["10.0.0.2/32", "fd00::2/128"],
    "hysteria_auth": "hy-auth",
    "masque_pass": "masque-pass",
}


class CreateProxyWithoutMtprotoTest(unittest.TestCase):
    def test_no_args_leaves_mtproto_unset(self):
        proxy = create_proxy()

        self.assertFalse(proxy.HasField("mtproto"))

    def test_no_args_serializes_like_before(self):
        proxy = create_proxy()

        self.assertEqual(proxy, legacy_proxy())
        self.assertEqual(
            proxy.SerializeToString(deterministic=True),
            legacy_proxy().SerializeToString(deterministic=True),
        )

    def test_existing_fields_serialize_like_before(self):
        proxy = create_proxy(**FULL_LEGACY_ARGS)

        self.assertFalse(proxy.HasField("mtproto"))
        self.assertEqual(proxy, legacy_proxy(**FULL_LEGACY_ARGS))
        self.assertEqual(
            proxy.SerializeToString(deterministic=True),
            legacy_proxy(**FULL_LEGACY_ARGS).SerializeToString(deterministic=True),
        )

    def test_empty_secret_leaves_mtproto_unset(self):
        proxy = create_proxy(
            mtproto_secret="",
            mtproto_user_ad_tag="0123456789abcdef0123456789abcdef",
            mtproto_max_tcp_conns=4,
            mtproto_max_unique_ips=2,
        )

        self.assertFalse(proxy.HasField("mtproto"))
        self.assertEqual(proxy, legacy_proxy())

    def test_limits_without_secret_leave_mtproto_unset(self):
        proxy = create_proxy(mtproto_user_ad_tag="tag", mtproto_max_tcp_conns=4, mtproto_max_unique_ips=2)

        self.assertFalse(proxy.HasField("mtproto"))
        self.assertEqual(proxy, legacy_proxy())

    def test_blank_secret_leaves_mtproto_unset(self):
        for secret in (" ", "   ", "\t", "\n", " \t\r\n "):
            with self.subTest(secret=secret):
                proxy = create_proxy(
                    mtproto_secret=secret,
                    mtproto_user_ad_tag="0123456789abcdef0123456789abcdef",
                    mtproto_max_tcp_conns=4,
                    mtproto_max_unique_ips=2,
                )

                self.assertFalse(proxy.HasField("mtproto"))
                self.assertEqual(
                    proxy.SerializeToString(deterministic=True),
                    legacy_proxy().SerializeToString(deterministic=True),
                )


class CreateProxyWithMtprotoTest(unittest.TestCase):
    def test_secret_sets_all_mtproto_fields(self):
        proxy = create_proxy(
            mtproto_secret=MTPROTO_SECRET,
            mtproto_user_ad_tag="0123456789abcdef0123456789abcdef",
            mtproto_max_tcp_conns=8,
            mtproto_max_unique_ips=3,
        )

        self.assertTrue(proxy.HasField("mtproto"))
        self.assertEqual(proxy.mtproto.secret, MTPROTO_SECRET)
        self.assertEqual(proxy.mtproto.user_ad_tag, "0123456789abcdef0123456789abcdef")
        self.assertEqual(proxy.mtproto.max_tcp_conns, 8)
        self.assertEqual(proxy.mtproto.max_unique_ips, 3)

    def test_secret_alone_uses_zero_limits_and_empty_ad_tag(self):
        proxy = create_proxy(mtproto_secret=MTPROTO_SECRET)

        self.assertTrue(proxy.HasField("mtproto"))
        self.assertEqual(proxy.mtproto, service.Mtproto(secret=MTPROTO_SECRET))
        self.assertEqual(proxy.mtproto.user_ad_tag, "")
        self.assertEqual(proxy.mtproto.max_tcp_conns, 0)
        self.assertEqual(proxy.mtproto.max_unique_ips, 0)

    def test_secret_value_is_sent_as_given(self):
        # The node trims the secret itself, so the bridge does not rewrite it.
        proxy = create_proxy(mtproto_secret=f" {MTPROTO_SECRET} ")

        self.assertTrue(proxy.HasField("mtproto"))
        self.assertEqual(proxy.mtproto.secret, f" {MTPROTO_SECRET} ")

    def test_limit_args_default_to_none(self):
        parameters = inspect.signature(create_proxy).parameters

        for name in ("mtproto_max_tcp_conns", "mtproto_max_unique_ips"):
            with self.subTest(name=name):
                self.assertIsNone(parameters[name].default)
                self.assertEqual(parameters[name].annotation, int | None)

    def test_none_limits_send_zero(self):
        # The panel passes dict.get() values, so a missing limit arrives as None.
        settings = {"secret": MTPROTO_SECRET}
        proxy = create_proxy(
            mtproto_secret=settings.get("secret"),
            mtproto_user_ad_tag=settings.get("user_ad_tag"),
            mtproto_max_tcp_conns=settings.get("max_tcp_conns"),
            mtproto_max_unique_ips=settings.get("max_unique_ips"),
        )

        self.assertTrue(proxy.HasField("mtproto"))
        self.assertEqual(proxy.mtproto.max_tcp_conns, 0)
        self.assertEqual(proxy.mtproto.max_unique_ips, 0)
        self.assertEqual(
            proxy.SerializeToString(deterministic=True),
            create_proxy(
                mtproto_secret=MTPROTO_SECRET, mtproto_max_tcp_conns=0, mtproto_max_unique_ips=0
            ).SerializeToString(deterministic=True),
        )

    def test_mtproto_does_not_change_other_fields(self):
        proxy = create_proxy(**FULL_LEGACY_ARGS, mtproto_secret=MTPROTO_SECRET)

        proxy.ClearField("mtproto")
        self.assertEqual(proxy, legacy_proxy(**FULL_LEGACY_ARGS))

    def test_mtproto_round_trips(self):
        proxy = create_proxy(
            vless_id="1e6a379b-a958-4329-bf1a-76419fc63f19",
            mtproto_secret=MTPROTO_SECRET,
            mtproto_user_ad_tag="tag",
            mtproto_max_tcp_conns=8,
            mtproto_max_unique_ips=3,
        )

        decoded = service.Proxy.FromString(proxy.SerializeToString())

        self.assertTrue(decoded.HasField("mtproto"))
        self.assertEqual(decoded, proxy)


class WireFormatTest(unittest.TestCase):
    def test_proxy_field_numbers(self):
        fields = service.Proxy.DESCRIPTOR.fields_by_name

        self.assertEqual(fields["masque"].number, 7)
        self.assertEqual(fields["mtproto"].number, 8)

    def test_mtproto_uses_field_8_on_the_wire(self):
        mtproto = service.Mtproto(secret=MTPROTO_SECRET, user_ad_tag="tag", max_tcp_conns=8, max_unique_ips=3)
        payload = mtproto.SerializeToString()

        data = service.Proxy(mtproto=mtproto).SerializeToString()

        # 0x42 = field 8, wire type 2 (length delimited)
        self.assertEqual(data[0], 0x42)
        self.assertEqual(data, b"\x42" + bytes([len(payload)]) + payload)

    def test_masque_stays_on_field_7(self):
        data = service.Proxy(masque=service.Masque(**{"pass": "masque-pass"})).SerializeToString()

        # 0x3a = field 7, wire type 2 (length delimited)
        self.assertEqual(data[0], 0x3A)

    def test_create_proxy_appends_mtproto_after_existing_fields(self):
        with_mtproto = create_proxy(**FULL_LEGACY_ARGS, mtproto_secret=MTPROTO_SECRET)
        payload = service.Mtproto(secret=MTPROTO_SECRET).SerializeToString()

        data = with_mtproto.SerializeToString(deterministic=True)
        legacy = legacy_proxy(**FULL_LEGACY_ARGS).SerializeToString(deterministic=True)

        self.assertEqual(data, legacy + b"\x42" + bytes([len(payload)]) + payload)

    def test_mtproto_field_numbers(self):
        fields = service.Mtproto.DESCRIPTOR.fields_by_name

        self.assertEqual(fields["secret"].number, 1)
        self.assertEqual(fields["user_ad_tag"].number, 2)
        self.assertEqual(fields["max_tcp_conns"].number, 3)
        self.assertEqual(fields["max_unique_ips"].number, 4)


class BackendTypeTest(unittest.TestCase):
    def test_backend_type_values(self):
        self.assertEqual(service.BackendType.XRAY, 0)
        self.assertEqual(service.BackendType.WIREGUARD, 1)
        self.assertEqual(service.BackendType.MTPROTO, 2)
        self.assertEqual(service.BackendType.Name(2), "MTPROTO")

    def test_backend_carries_mtproto_type(self):
        backend = service.Backend(type=service.BackendType.MTPROTO, config="{}")

        self.assertEqual(service.Backend.FromString(backend.SerializeToString()).type, service.BackendType.MTPROTO)


class WireguardPreSharedKeyTest(unittest.TestCase):
    def test_pre_shared_key_round_trips(self):
        wireguard = service.Wireguard(public_key="wg-public-key", peer_ips=["10.0.0.2/32"], pre_shared_key="wg-psk")

        decoded = service.Wireguard.FromString(wireguard.SerializeToString())

        self.assertEqual(decoded.pre_shared_key, "wg-psk")
        self.assertEqual(decoded, wireguard)
        self.assertEqual(service.Wireguard.DESCRIPTOR.fields_by_name["pre_shared_key"].number, 3)


if __name__ == "__main__":
    unittest.main()
