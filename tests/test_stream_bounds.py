import asyncio
import logging
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PasarGuardNodeBridge.common import service_pb2 as service
from PasarGuardNodeBridge.grpclib import Node as GrpcNode
from PasarGuardNodeBridge.storage import InMemoryUserSyncStore


class _HangingStream:
    def __init__(self, hang_on: str):
        self.hang_on = hang_on
        self.sent = []

    async def __aenter__(self):
        if self.hang_on == "open":
            await asyncio.Event().wait()
        return self

    async def __aexit__(self, *exc):
        return False

    async def send_message(self, message):
        self.sent.append(message)

    async def end(self):
        if self.hang_on == "end":
            await asyncio.Event().wait()

    async def recv_message(self):
        return service.Empty()


class StreamBoundTests(unittest.IsolatedAsyncioTestCase):
    def make_node(self, hang_on: str):
        with patch("ssl.SSLContext.load_verify_locations"):
            node = GrpcNode(
                address="127.0.0.1",
                port=1,
                api_port=1,
                server_ca="test",
                api_key="00000000-0000-0000-0000-000000000000",
                node_id="bounded",
                user_sync_store=InMemoryUserSyncStore(),
                internal_timeout=1,
                default_timeout=1,
                logger=logging.getLogger("bounded-test"),
            )
        node._internal_timeout = 0.2
        node._default_timeout = 0.2
        stream = _HangingStream(hang_on)
        node._client = SimpleNamespace(
            SyncUsersChunked=SimpleNamespace(open=lambda **kwargs: stream),
            SyncUser=SimpleNamespace(open=lambda **kwargs: stream),
        )
        return node

    async def assert_bounded(self, call, users):
        started = time.monotonic()
        failed = await asyncio.wait_for(call(users), timeout=5)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual([user.email for user in failed], [user.email for user in users])

    async def test_a_chunked_stream_whose_end_hangs_returns_its_users_as_failed(self):
        node = self.make_node("end")
        users = [service.User(email=str(index)) for index in range(3)]
        await self.assert_bounded(lambda batch: node.sync_users_chunked(batch, chunk_size=2), users)
        self.assertFalse(node._node_lock.locked())

    async def test_a_chunked_stream_whose_open_hangs_returns_its_users_as_failed(self):
        node = self.make_node("open")
        users = [service.User(email="a")]
        await self.assert_bounded(lambda batch: node.sync_users_chunked(batch, chunk_size=2), users)
        self.assertFalse(node._node_lock.locked())

    async def test_a_per_user_stream_whose_end_hangs_returns_its_users_as_failed(self):
        node = self.make_node("end")
        users = [service.User(email="a"), service.User(email="b")]
        await self.assert_bounded(node._sync_batch_users, users)

    async def test_a_per_user_stream_whose_open_hangs_returns_its_users_as_failed(self):
        node = self.make_node("open")
        users = [service.User(email="a")]
        await self.assert_bounded(node._sync_batch_users, users)


if __name__ == "__main__":
    unittest.main()
