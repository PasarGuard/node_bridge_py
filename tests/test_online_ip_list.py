import logging
import unittest
from unittest.mock import patch

from aiohttp import web
from grpclib.client import Channel
from grpclib.const import Cardinality, Handler, Status
from grpclib.exceptions import GRPCError
from grpclib.server import Server

from PasarGuardNodeBridge.abstract_node import PasarGuardNode
from PasarGuardNodeBridge.aiohttp_compat import LazyClientSession, make_timeout
from PasarGuardNodeBridge.common import service_grpc
from PasarGuardNodeBridge.common import service_pb2 as service
from PasarGuardNodeBridge.controller import NodeAPIError
from PasarGuardNodeBridge.grpclib import Node as GrpcNode
from PasarGuardNodeBridge.rest import Node as RestNode
from PasarGuardNodeBridge.storage import InMemoryUserSyncStore
from PasarGuardNodeBridge.utils import grpc_to_http_status

ALL_USERS_RESPONSE = service.StatsAllOnlineIpListResponse(
    users={
        "alice@example.com": service.StatsOnlineIpListResponse(name="alice@example.com", ips={"10.0.0.1": 100}),
        "bob@example.com": service.StatsOnlineIpListResponse(
            name="bob@example.com", ips={"10.0.0.2": 200, "10.0.0.3": 300}
        ),
    }
)


class GrpcOnlineIpService:
    def __init__(self, response: service.StatsAllOnlineIpListResponse | None = None, error: GRPCError | None = None):
        self.response = response or service.StatsAllOnlineIpListResponse()
        self.error = error

    async def get_users_online_ip_list(self, stream) -> None:
        await stream.recv_message()
        if self.error is not None:
            raise self.error
        await stream.send_message(self.response)

    def __mapping__(self) -> dict[str, Handler]:
        return {
            "/service.NodeService/GetUsersOnlineIpListStats": Handler(
                self.get_users_online_ip_list,
                Cardinality.UNARY_UNARY,
                service.Empty,
                service.StatsAllOnlineIpListResponse,
            ),
        }


class OnlineIpListTests(unittest.IsolatedAsyncioTestCase):
    def make_node(self, node_class, port: int):
        with patch("ssl.SSLContext.load_verify_locations"):
            return node_class(
                address="127.0.0.1",
                port=port,
                api_port=1,
                server_ca="test",
                api_key="00000000-0000-0000-0000-000000000000",
                node_id="online-ip",
                user_sync_store=InMemoryUserSyncStore(),
                logger=logging.getLogger("online-ip-test"),
            )

    async def start_grpc(self, handler: GrpcOnlineIpService):
        server = Server([handler])
        await server.start("127.0.0.1", 0)
        port = server._server.sockets[0].getsockname()[1]
        node = self.make_node(GrpcNode, port)
        node.channel.close()
        node.channel = Channel("127.0.0.1", port, ssl=False)
        node._client = service_grpc.NodeServiceStub(node.channel)
        return server, node

    async def close_grpc(self, server: Server, node: GrpcNode) -> None:
        node.channel.close()
        await node._json_client.close()
        server.close()
        await server.wait_closed()

    async def test_grpc_returns_ips_of_all_users(self):
        server, node = await self.start_grpc(GrpcOnlineIpService(response=ALL_USERS_RESPONSE))
        try:
            result = await node.get_users_online_ip_list()
            self.assertEqual(result, ALL_USERS_RESPONSE)
            self.assertEqual(result.users["bob@example.com"].ips["10.0.0.3"], 300)
        finally:
            await self.close_grpc(server, node)

    async def test_grpc_empty_response_has_no_users(self):
        server, node = await self.start_grpc(GrpcOnlineIpService())
        try:
            result = await node.get_users_online_ip_list()
            self.assertEqual(result, service.StatsAllOnlineIpListResponse())
            self.assertEqual(len(result.users), 0)
        finally:
            await self.close_grpc(server, node)

    async def test_grpc_node_error_raises_node_api_error(self):
        error = GRPCError(Status.UNAVAILABLE, "node down")
        server, node = await self.start_grpc(GrpcOnlineIpService(error=error))
        try:
            with self.assertRaises(NodeAPIError) as ctx:
                await node.get_users_online_ip_list()
            self.assertEqual(ctx.exception.code, grpc_to_http_status(Status.UNAVAILABLE))
            self.assertEqual(ctx.exception.detail, "node down")
        finally:
            await self.close_grpc(server, node)

    async def start_rest(self, handler):
        app = web.Application()
        app.router.add_get("/stats/users/online_ip", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        node = self.make_node(RestNode, port)
        await node._client.close()
        node._client = LazyClientSession(
            ssl_context=None,
            headers={"Content-Type": "application/x-protobuf"},
            base_url=f"http://127.0.0.1:{port}/",
            timeout=make_timeout(None),
        )
        return runner, node

    async def close_rest(self, runner: web.AppRunner, node: RestNode) -> None:
        await node._client.close()
        await node._json_client.close()
        await runner.cleanup()

    async def test_rest_returns_ips_of_all_users(self):
        async def handler(request: web.Request) -> web.Response:
            return web.Response(body=ALL_USERS_RESPONSE.SerializeToString())

        runner, node = await self.start_rest(handler)
        try:
            result = await node.get_users_online_ip_list()
            self.assertEqual(result, ALL_USERS_RESPONSE)
            self.assertEqual(result.users["alice@example.com"].ips["10.0.0.1"], 100)
        finally:
            await self.close_rest(runner, node)

    async def test_rest_empty_response_has_no_users(self):
        async def handler(request: web.Request) -> web.Response:
            return web.Response(body=service.StatsAllOnlineIpListResponse().SerializeToString())

        runner, node = await self.start_rest(handler)
        try:
            result = await node.get_users_online_ip_list()
            self.assertEqual(result, service.StatsAllOnlineIpListResponse())
            self.assertEqual(len(result.users), 0)
        finally:
            await self.close_rest(runner, node)

    async def test_rest_node_error_raises_node_api_error(self):
        async def handler(request: web.Request) -> web.Response:
            return web.Response(status=500, text="boom")

        runner, node = await self.start_rest(handler)
        try:
            with self.assertRaises(NodeAPIError) as ctx:
                await node.get_users_online_ip_list()
            self.assertEqual(ctx.exception.code, 500)
        finally:
            await self.close_rest(runner, node)


class BaseClassDefaultTests(unittest.IsolatedAsyncioTestCase):
    async def test_bulk_method_is_not_abstract_so_existing_subclasses_still_instantiate(self):
        self.assertNotIn("get_users_online_ip_list", PasarGuardNode.__abstractmethods__)

    async def test_bulk_method_default_reports_it_is_not_implemented(self):
        with self.assertRaises(NodeAPIError) as ctx:
            await PasarGuardNode.get_users_online_ip_list(object())
        self.assertEqual(ctx.exception.code, 501)


if __name__ == "__main__":
    unittest.main()
