"""h2 closed-stream history retention tests (#1254).

A long-lived gRPC connection whose streams keep closing retains a bounded
per-connection history of closed streams. h2's default cap (MAX_CLOSED_STREAMS
= 65536) is far above what a panel<->node bridge ever needs to hold, so the
bridge must lower the per-channel retention without touching stream lifecycle
itself.

The test drives a real local plaintext gRPC server through the bridge's own
`ProxiedChannel` with a pass-through proxy dialer, then asserts the h2
connection's `_closed_streams` stays at the configured cap.
"""

import asyncio
import logging
import unittest
from unittest.mock import patch

from grpclib.const import Cardinality, Handler
from grpclib.server import Server

import PasarGuardNodeBridge.grpclib as bridge_grpclib
from PasarGuardNodeBridge.common import service_pb2 as service
from PasarGuardNodeBridge.common import service_grpc
from PasarGuardNodeBridge.grpclib import Node as GrpcNode, ProxiedChannel
from PasarGuardNodeBridge.storage import InMemoryUserSyncStore


class _DirectProxy:
    """Pass-through stand-in for python-socks Proxy: dials the destination directly."""

    def __init__(self, *args, **kwargs):
        pass

    @classmethod
    def from_url(cls, url: str, rdns=None, **kwargs):
        return cls()

    async def connect(self, dest_host, dest_port, timeout=None, **kwargs):
        import socket

        sock = socket.create_connection((dest_host, dest_port))
        sock.setblocking(False)
        return sock


class ClosedStreamsCapTests(unittest.IsolatedAsyncioTestCase):
    def make_node(self, port, **kwargs):
        with patch("ssl.SSLContext.load_verify_locations"):
            return GrpcNode(
                address="127.0.0.1",
                port=port,
                api_port=1,
                server_ca="test",
                api_key="00000000-0000-0000-0000-000000000000",
                node_id="closed-streams-cap",
                user_sync_store=InMemoryUserSyncStore(),
                sync_poll_interval=0.01,
                logger=logging.getLogger("closed-streams-test"),
                **kwargs,
            )

    async def test_lowered_cap_bounds_closed_stream_history(self):
        """With max_closed_streams set, the retained closed-stream history stays at the cap."""

        class Receiver:
            async def sync_users(inner, stream):
                await stream.recv_message()
                await stream.send_message(service.Empty())

            def __mapping__(inner):
                return {
                    "/service.NodeService/SyncUsers": Handler(
                        inner.sync_users, Cardinality.UNARY_UNARY, service.Users, service.Empty
                    ),
                }

        server = Server([Receiver()])
        await server.start("127.0.0.1", 0)
        port = server._server.sockets[0].getsockname()[1]

        cap = 8
        node = self.make_node(port, max_closed_streams=cap)
        node._close_chan()
        with patch.object(bridge_grpclib, "Proxy", _DirectProxy):
            node.channel = ProxiedChannel(
                "127.0.0.1", port, ssl=False, proxy_url="socks5://127.0.0.1:1",
                max_closed_streams=cap,
            )
            node._client = service_grpc.NodeServiceStub(node.channel)
            try:
                for _ in range(cap * 4):
                    await node._client.SyncUsers(
                        service.Users(users=[service.User(email="u0@x")]),
                        metadata=node._metadata,
                    )
                h2_conn = node.channel._protocol.connection._connection
                self.assertLessEqual(len(h2_conn._closed_streams), cap)
            finally:
                node._close_chan()
        server.close()
        await server.wait_closed()

    async def test_default_keeps_h2_behavior(self):
        """Without the kwarg the cap is the bridge default (4096), not h2's 65536."""
        node = self.make_node(1)
        node._close_chan()
        node.channel = ProxiedChannel(
            "127.0.0.1", 1, ssl=False, proxy_url="socks5://127.0.0.1:1"
        )
        self.assertEqual(node.channel._max_closed_streams, bridge_grpclib.DEFAULT_MAX_CLOSED_STREAMS)
        node._close_chan()


if __name__ == "__main__":
    unittest.main()
