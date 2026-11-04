import asyncio
import logging
import time
import unittest
from dataclasses import dataclass
from unittest.mock import patch

from grpclib import protocol
from grpclib.client import Channel
from grpclib.const import Cardinality, Handler
from grpclib.server import Server
from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import StreamEnded
from h2.settings import SettingCodes

from PasarGuardNodeBridge.common import service_grpc
from PasarGuardNodeBridge.common import service_pb2 as service
from PasarGuardNodeBridge.grpclib import Node as GrpcNode
from PasarGuardNodeBridge.storage import InMemoryUserSyncStore

STEP_TIMEOUT = 1.0
OUTER_GUARD = 12
LARGEST_FRAME = 2**24 - 1
LARGEST_WINDOW = 2**31 - 1
DEFAULT_WINDOW = 65535
LARGE_INBOUND = "x" * (15 * 1024 * 1024)
EMPTY_GRPC_MESSAGE = b"\x00\x00\x00\x00\x00"


@dataclass
class EndCall:
    ready_on_entry: bool
    started: float
    outcome: str = "pending"
    ready_on_exit: bool | None = None
    pending_for: float | None = None

    def close(self, outcome: str, stream: protocol.Stream):
        self.outcome = outcome
        self.ready_on_exit = stream.connection.write_ready.is_set()
        self.pending_for = time.monotonic() - self.started


class NodeThatNeverFinishes:
    def __init__(self, sends_reply: bool):
        self.sends_reply = sends_reply
        self.release = asyncio.Event()

    async def drain_then_wait(self, stream):
        while await stream.recv_message() is not None:
            pass
        if self.sends_reply:
            await stream.send_message(service.Empty())
        await self.release.wait()

    def __mapping__(self):
        return {
            "/service.NodeService/SyncUsersChunked": Handler(
                self.drain_then_wait, Cardinality.STREAM_UNARY, service.UsersChunk, service.Empty
            ),
            "/service.NodeService/SyncUser": Handler(
                self.drain_then_wait, Cardinality.STREAM_UNARY, service.User, service.Empty
            ),
        }


class PeerThatStopsReading:
    def __init__(self, resume_after: float | None):
        self.resume_after = resume_after
        self.release = asyncio.Event()
        self.server = None

    async def start(self) -> int:
        self.server = await asyncio.start_server(self.serve, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def close(self):
        self.release.set()
        self.server.close()
        await asyncio.wait_for(self.server.wait_closed(), timeout=OUTER_GUARD)

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        connection = H2Connection(H2Configuration(client_side=False))
        connection.initiate_connection()
        connection.update_settings(
            {SettingCodes.MAX_FRAME_SIZE: LARGEST_FRAME, SettingCodes.INITIAL_WINDOW_SIZE: LARGEST_WINDOW}
        )
        connection.increment_flow_control_window(LARGEST_WINDOW - DEFAULT_WINDOW)
        writer.write(connection.data_to_send())
        try:
            while connection.local_settings.max_frame_size != LARGEST_FRAME:
                data = await reader.read(65536)
                if not data:
                    return
                connection.receive_data(data)
                writer.write(connection.data_to_send())
            writer.transport.pause_reading()
            if self.resume_after is None:
                await self.release.wait()
                return
            await asyncio.sleep(self.resume_after)
            writer.transport.resume_reading()
            stream_id = None
            while stream_id is None:
                data = await reader.read(1024 * 1024)
                if not data:
                    return
                for event in connection.receive_data(data):
                    if isinstance(event, StreamEnded):
                        stream_id = event.stream_id
            connection.send_headers(stream_id, [(":status", "200"), ("content-type", "application/grpc")])
            connection.send_data(stream_id, EMPTY_GRPC_MESSAGE)
            connection.send_headers(stream_id, [("grpc-status", "0")], end_stream=True)
            writer.write(connection.data_to_send())
            await writer.drain()
            await self.release.wait()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass


class StreamBoundTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.end_calls = []
        real_end = protocol.Stream.end
        end_calls = self.end_calls

        async def watched_end(stream: protocol.Stream):
            call = EndCall(ready_on_entry=stream.connection.write_ready.is_set(), started=time.monotonic())
            end_calls.append(call)
            try:
                await real_end(stream)
            except asyncio.CancelledError:
                call.close("cancelled", stream)
                raise
            call.close("returned", stream)

        self.enterContext(patch.object(protocol.Stream, "end", watched_end))

    def connect_node(self, port: int) -> GrpcNode:
        with patch("ssl.SSLContext.load_verify_locations"):
            node = GrpcNode(
                address="127.0.0.1",
                port=port,
                api_port=port,
                server_ca="test",
                api_key="00000000-0000-0000-0000-000000000000",
                node_id="bounded",
                user_sync_store=InMemoryUserSyncStore(),
                internal_timeout=1,
                default_timeout=1,
                logger=logging.getLogger("bounded-test"),
            )
        node._internal_timeout = STEP_TIMEOUT
        node._default_timeout = STEP_TIMEOUT
        node.channel = Channel("127.0.0.1", port)
        node._client = service_grpc.NodeServiceStub(node.channel)
        self.addAsyncCleanup(self.settle)
        self.addCleanup(node.channel.close)
        return node

    async def settle(self):
        await asyncio.sleep(0.05)

    async def start_node_that_never_finishes(self, sends_reply: bool) -> GrpcNode:
        peer = NodeThatNeverFinishes(sends_reply)
        server = Server([peer])
        await server.start("127.0.0.1", 0)
        port = server._server.sockets[0].getsockname()[1]

        async def stop():
            peer.release.set()
            server.close()
            await asyncio.wait_for(server.wait_closed(), timeout=OUTER_GUARD)

        self.addAsyncCleanup(stop)
        return self.connect_node(port)

    async def start_peer_that_stops_reading(self, resume_after: float | None) -> GrpcNode:
        peer = PeerThatStopsReading(resume_after)
        port = await peer.start()
        self.addAsyncCleanup(peer.close)
        return self.connect_node(port)

    async def run_guarded(self, call, users):
        started = time.monotonic()
        result = await asyncio.wait_for(call(users), timeout=OUTER_GUARD)
        return result, time.monotonic() - started

    def assert_all_failed(self, result, users):
        self.assertEqual([user.email for user in result], [user.email for user in users])

    def only_end_call(self) -> EndCall:
        self.assertEqual(len(self.end_calls), 1)
        return self.end_calls[0]

    def assert_end_returned_at_once(self):
        call = self.only_end_call()
        self.assertTrue(call.ready_on_entry)
        self.assertEqual(call.outcome, "returned")

    def assert_end_stayed_blocked_on_flow_control(self):
        call = self.only_end_call()
        self.assertFalse(call.ready_on_entry)
        self.assertEqual(call.outcome, "cancelled")
        self.assertFalse(call.ready_on_exit)

    def assert_end_waited_on_flow_control_longer_than_one_internal_timeout(self):
        call = self.only_end_call()
        self.assertFalse(call.ready_on_entry)
        self.assertEqual(call.outcome, "returned")
        self.assertGreater(call.pending_for, STEP_TIMEOUT)

    async def test_a_chunked_sync_returns_when_the_node_never_sends_trailers(self):
        node = await self.start_node_that_never_finishes(sends_reply=True)
        users = [service.User(email=str(index)) for index in range(3)]
        result, elapsed = await self.run_guarded(lambda batch: node.sync_users_chunked(batch, chunk_size=2), users)
        self.assert_all_failed(result, users)
        self.assertFalse(result.rejected)
        self.assertFalse(node._node_lock.locked())
        self.assertLess(elapsed, STEP_TIMEOUT * 6)
        self.assert_end_returned_at_once()

    async def test_a_per_user_sync_returns_when_the_node_never_answers(self):
        node = await self.start_node_that_never_finishes(sends_reply=False)
        users = [service.User(email="a"), service.User(email="b")]
        result, elapsed = await self.run_guarded(node._sync_batch_users, users)
        self.assert_all_failed(result, users)
        self.assertLess(elapsed, STEP_TIMEOUT * 6)
        self.assert_end_returned_at_once()

    async def test_a_chunked_sync_returns_when_the_peer_stops_reading_during_end(self):
        node = await self.start_peer_that_stops_reading(resume_after=None)
        users = [service.User(email="large", inbounds=[LARGE_INBOUND])]
        result, elapsed = await self.run_guarded(lambda batch: node.sync_users_chunked(batch, chunk_size=1), users)
        self.assert_all_failed(result, users)
        self.assertFalse(result.rejected)
        self.assertFalse(node._node_lock.locked())
        self.assertLess(elapsed, STEP_TIMEOUT * 6)
        self.assert_end_stayed_blocked_on_flow_control()

    async def test_a_per_user_sync_returns_when_the_peer_stops_reading_during_end(self):
        node = await self.start_peer_that_stops_reading(resume_after=None)
        users = [service.User(email="large", inbounds=[LARGE_INBOUND])]
        result, elapsed = await self.run_guarded(node._sync_batch_users, users)
        self.assert_all_failed(result, users)
        self.assertLess(elapsed, STEP_TIMEOUT * 6)
        self.assert_end_stayed_blocked_on_flow_control()

    async def test_a_chunked_sync_survives_a_pause_in_end_longer_than_one_internal_timeout(self):
        node = await self.start_peer_that_stops_reading(resume_after=STEP_TIMEOUT * 1.5)
        users = [service.User(email="large", inbounds=[LARGE_INBOUND])]
        result, _ = await self.run_guarded(lambda batch: node.sync_users_chunked(batch, chunk_size=1), users)
        self.assertEqual([user.email for user in result], [])
        self.assertFalse(node._node_lock.locked())
        self.assert_end_waited_on_flow_control_longer_than_one_internal_timeout()

    async def test_a_per_user_sync_survives_a_pause_in_end_longer_than_one_internal_timeout(self):
        node = await self.start_peer_that_stops_reading(resume_after=STEP_TIMEOUT * 1.5)
        users = [service.User(email="large", inbounds=[LARGE_INBOUND])]
        result, _ = await self.run_guarded(node._sync_batch_users, users)
        self.assertEqual([user.email for user in result], [])
        self.assert_end_waited_on_flow_control_longer_than_one_internal_timeout()


if __name__ == "__main__":
    unittest.main()
