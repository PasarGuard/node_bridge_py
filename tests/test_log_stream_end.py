import asyncio
import contextlib
import logging
import unittest
from unittest.mock import patch

from aiohttp import web
from grpclib.client import Channel, Stream
from grpclib.const import Cardinality, Handler, Status
from grpclib.exceptions import GRPCError
from grpclib.server import Server

from PasarGuardNodeBridge import NodeAPIError
from PasarGuardNodeBridge.aiohttp_compat import LazyClientSession, make_timeout
from PasarGuardNodeBridge.common import service_grpc
from PasarGuardNodeBridge.common import service_pb2 as service
from PasarGuardNodeBridge.grpclib import Node as GrpcNode
from PasarGuardNodeBridge.rest import Node as RestNode
from PasarGuardNodeBridge.storage import InMemoryUserSyncStore

LOGGER = "log-stream-end"


class LogSource:
    def __init__(self):
        self.lines = []
        self.ending = "return"
        self.hold = asyncio.Event()
        self.opened = asyncio.Event()


class GrpcLogNode:
    def __init__(self, source):
        self.source = source

    async def logs(self, stream):
        await stream.recv_message()
        self.source.opened.set()
        for line in self.source.lines:
            await stream.send_message(service.Log(detail=line))
        if self.source.ending == "hold":
            await self.source.hold.wait()
        if self.source.ending == "error":
            raise GRPCError(Status.UNAVAILABLE, "core log channel closed")

    def __mapping__(self):
        return {
            "/service.NodeService/GetLogs": Handler(self.logs, Cardinality.UNARY_STREAM, service.Empty, service.Log)
        }


class DroppingRelay:
    def __init__(self, upstream_port):
        self.upstream_port = upstream_port
        self.writers = []
        self.server = None

    async def _pump(self, reader, writer):
        with contextlib.suppress(Exception):
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()

    async def _handle(self, client_reader, client_writer):
        upstream_reader, upstream_writer = await asyncio.open_connection("127.0.0.1", self.upstream_port)
        self.writers += [client_writer, upstream_writer]
        await asyncio.gather(self._pump(client_reader, upstream_writer), self._pump(upstream_reader, client_writer))

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    def drop(self):
        for writer in self.writers:
            writer.close()

    def close(self):
        self.drop()
        self.server.close()


class LogStreamEndTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cleanups = []

    async def asyncTearDown(self):
        for cleanup in reversed(self.cleanups):
            await cleanup()

    def make_node(self, node_class, port):
        with patch("ssl.SSLContext.load_verify_locations"):
            return node_class(
                address="127.0.0.1",
                port=port,
                api_port=port,
                server_ca="test",
                api_key="00000000-0000-0000-0000-000000000000",
                name="logs",
                node_id="logs",
                user_sync_store=InMemoryUserSyncStore(),
                logger=logging.getLogger(LOGGER),
            )

    async def grpc_node(self, source):
        server = Server([GrpcLogNode(source)])
        await server.start("127.0.0.1", 0)
        relay = DroppingRelay(server._server.sockets[0].getsockname()[1])
        port = await relay.start()
        self.relay = relay
        node = self.make_node(GrpcNode, port)
        node.channel.close()
        node.channel = Channel(host="127.0.0.1", port=port)
        node._client = service_grpc.NodeServiceStub(node.channel)

        async def cleanup():
            source.hold.set()
            node._close_chan()
            await node._json_client.close()
            relay.close()
            server.close()
            await asyncio.wait_for(server.wait_closed(), 5)

        self.cleanups.append(cleanup)
        return node

    async def rest_node(self, source):
        transports = []

        async def logs(request):
            response = web.StreamResponse()
            await response.prepare(request)
            source.opened.set()
            transports.append(request.transport)
            for line in source.lines:
                await response.write(f"{line}\n".encode())
            if source.ending == "hold":
                await source.hold.wait()
            if source.ending == "drop":
                request.transport.close()
                return response
            await response.write_eof()
            return response

        app = web.Application()
        app.router.add_get("/logs", logs)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        node = self.make_node(RestNode, port)
        await node._client.close()
        node._client = LazyClientSession(
            ssl_context=None, headers={}, base_url=f"http://127.0.0.1:{port}/", timeout=make_timeout(None)
        )
        self.transports = transports

        async def cleanup():
            source.hold.set()
            await node._client.close()
            await node._json_client.close()
            await runner.cleanup()

        self.cleanups.append(cleanup)
        return node

    async def read_until_error(self, queue, timeout=5):
        items = []
        async with asyncio.timeout(timeout):
            while True:
                item = await queue.get()
                items.append(item)
                if isinstance(item, NodeAPIError):
                    return items

    async def assert_terminal_error(self, node, expect_lines):
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            async with node.stream_logs() as queue:
                items = await self.read_until_error(queue)
        self.assertEqual(items[:-1], expect_lines)
        self.assertIsInstance(items[-1], NodeAPIError)
        self.assertTrue(any("log stream" in line.lower() for line in logs.output), logs.output)

    async def test_a_grpc_log_stream_the_node_ends_reaches_the_caller_as_an_error(self):
        source = LogSource()
        source.lines = ["one", "two"]
        node = await self.grpc_node(source)
        await self.assert_terminal_error(node, ["one", "two"])

    async def test_a_grpc_log_stream_cut_by_the_connection_reaches_the_caller_as_an_error(self):
        source = LogSource()
        source.lines = ["one"]
        source.ending = "hold"
        node = await self.grpc_node(source)
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            async with node.stream_logs() as queue:
                self.assertEqual(await asyncio.wait_for(queue.get(), 5), "one")
                self.relay.drop()
                items = await self.read_until_error(queue)
        self.assertIsInstance(items[-1], NodeAPIError)
        self.assertTrue(any("log stream" in line.lower() for line in logs.output), logs.output)

    async def test_a_grpc_log_stream_failing_with_a_status_still_reaches_the_caller(self):
        source = LogSource()
        source.lines = ["one"]
        source.ending = "error"
        node = await self.grpc_node(source)
        async with node.stream_logs() as queue:
            items = await self.read_until_error(queue)
        self.assertEqual(items[0], "one")
        self.assertIsInstance(items[-1], NodeAPIError)

    async def test_a_rest_log_stream_the_node_ends_reaches_the_caller_as_an_error(self):
        source = LogSource()
        source.lines = ["one", "two"]
        node = await self.rest_node(source)
        await self.assert_terminal_error(node, ["one", "two"])

    async def test_a_rest_log_stream_cut_by_the_connection_reaches_the_caller_as_an_error(self):
        source = LogSource()
        source.lines = ["one"]
        source.ending = "drop"
        node = await self.rest_node(source)
        await self.assert_terminal_error(node, ["one"])

    async def test_the_terminal_error_is_not_lost_when_the_queue_is_full(self):
        for make in (self.grpc_node, self.rest_node):
            with self.subTest(transport=make.__name__):
                source = LogSource()
                source.lines = [f"line-{index}" for index in range(6)]
                node = await make(source)
                async with node.stream_logs(max_queue_size=2) as queue:
                    await asyncio.wait_for(source.opened.wait(), 5)
                    await asyncio.sleep(0.5)
                    items = await self.read_until_error(queue)
                self.assertIsInstance(items[-1], NodeAPIError)

    async def test_leaving_the_stream_is_silent(self):
        for make in (self.grpc_node, self.rest_node):
            with self.subTest(transport=make.__name__):
                source = LogSource()
                source.lines = ["one"]
                source.ending = "hold"
                node = await make(source)
                with self.assertNoLogs(LOGGER, level="WARNING"):
                    async with node.stream_logs() as queue:
                        self.assertEqual(await asyncio.wait_for(queue.get(), 5), "one")
                    await asyncio.sleep(0.2)
                self.assertTrue(queue.empty())

    async def test_leaving_stays_silent_even_when_the_cancel_lets_the_reader_run_first(self):
        source = LogSource()
        source.lines = ["one"]
        source.ending = "hold"
        node = await self.grpc_node(source)
        original = Stream.cancel

        async def slow_cancel(stream):
            await original(stream)
            await asyncio.sleep(0.2)

        with patch.object(Stream, "cancel", slow_cancel), self.assertNoLogs(LOGGER, level="WARNING"):
            async with node.stream_logs() as queue:
                self.assertEqual(await asyncio.wait_for(queue.get(), 5), "one")
        self.assertTrue(queue.empty())


if __name__ == "__main__":
    unittest.main()
