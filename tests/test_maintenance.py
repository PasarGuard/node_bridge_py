"""Maintenance calls go to node-serviced, which runs pg-node commands that take minutes."""

import asyncio
import logging
import unittest
from unittest.mock import patch

from aiohttp import web

from PasarGuardNodeBridge import controller
from PasarGuardNodeBridge.aiohttp_compat import LazyClientSession, make_timeout
from PasarGuardNodeBridge.controller import Health, NodeAPIError
from PasarGuardNodeBridge.grpclib import Node as GrpcNode
from PasarGuardNodeBridge.rest import Node as RestNode
from PasarGuardNodeBridge.storage import InMemoryNodeLifecycleCoordinator, InMemoryUserSyncStore, LifecycleOperation

SLOW_COMMAND_SECONDS = 1.5
API_KEY = "00000000-0000-0000-0000-000000000000"


class FlakyHeartbeatCoordinator(InMemoryNodeLifecycleCoordinator):
    """Fails the first lease heartbeat, then renews normally."""

    def __init__(self):
        super().__init__()
        self.heartbeats = 0

    async def heartbeat(self, lease):
        self.heartbeats += 1
        if self.heartbeats == 1:
            raise RuntimeError("store temporarily unavailable")
        return await super().heartbeat(lease)


class LostLeaseCoordinator(InMemoryNodeLifecycleCoordinator):
    """Reports the lease as gone on every heartbeat."""

    def __init__(self):
        super().__init__()
        self.heartbeats = 0

    async def heartbeat(self, lease):
        self.heartbeats += 1
        await self.release(lease)
        return False


class MaintenanceTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.command_delay = SLOW_COMMAND_SECONDS
        self.command_status = 200
        self.command_body = {"detail": "command succeeded"}
        self.requests = []

        @web.middleware
        async def authenticate(request, handler):
            if request.headers.get("x-api-key") != API_KEY:
                return web.json_response({"detail": "invalid api key"}, status=401)
            return await handler(request)

        async def root(request):
            return web.json_response({"status": "ok"})

        async def slow_command(request):
            body = await request.json() if request.can_read_body else None
            self.requests.append((request.method, request.path, body))
            await asyncio.sleep(self.command_delay)
            return web.json_response(self.command_body, status=self.command_status)

        app = web.Application(middlewares=[authenticate])
        app.router.add_get("/", root)
        for path in ("/node/update", "/node/core_update", "/node/geofiles", "/node/hard_reset"):
            app.router.add_post(path, slow_command)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", 0).start()
        self.api_port = self.runner.addresses[0][1]
        self.nodes = []

    async def asyncTearDown(self):
        for node in self.nodes:
            await node._json_client.close()
            if isinstance(node, GrpcNode):
                node._close_chan()
            else:
                await node._client.close()
        await self.runner.cleanup()

    def make_node(self, node_class=RestNode, **kwargs):
        with patch("ssl.SSLContext.load_verify_locations"):
            node = node_class(
                address="127.0.0.1",
                port=1,
                api_port=self.api_port,
                server_ca="test",
                api_key=API_KEY,
                node_id=f"maintenance-{len(self.nodes)}",
                user_sync_store=InMemoryUserSyncStore(),
                default_timeout=1,
                logger=logging.getLogger("maintenance-test"),
                **kwargs,
            )
        # Talk plain HTTP to the local stand-in for node-serviced.
        node._json_client = LazyClientSession(
            ssl_context=None,
            headers=node._json_client._headers,
            base_url=f"http://127.0.0.1:{self.api_port}",
            timeout=make_timeout(30),
        )
        self.nodes.append(node)
        return node

    async def test_serviced_routes_for_both_transports(self):
        self.command_delay = 0
        for node_class in (RestNode, GrpcNode):
            with self.subTest(transport=node_class.__module__):
                node = self.make_node(node_class)
                self.assertTrue(await node.check_connectivity())
                responses = [
                    await node.update_node(),
                    await node.update_core({"core_version": "latest"}),
                    await node.update_geofiles({"region": "iran"}),
                ]
                await node.set_health(Health.HEALTHY)
                responses.append(await node.hard_reset())
                self.assertEqual(await node.get_health(), Health.NOT_CONNECTED)
                for response in responses:
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json(), self.command_body)
                self.assertEqual(
                    self.requests[-4:],
                    [
                        ("POST", "/node/update", None),
                        ("POST", "/node/core_update", {"core_version": "latest"}),
                        ("POST", "/node/geofiles", {"region": "iran"}),
                        ("POST", "/node/hard_reset", None),
                    ],
                )

    async def test_failed_hard_reset_preserves_health_and_releases_lease(self):
        self.command_delay = 0
        self.command_status = 500
        self.command_body = {"detail": "hard reset failed for pg-node: restart failed"}
        for node_class in (RestNode, GrpcNode):
            with self.subTest(transport=node_class.__module__):
                coordinator = InMemoryNodeLifecycleCoordinator()
                node = self.make_node(node_class, lifecycle_coordinator=coordinator)
                await node.set_health(Health.HEALTHY)
                with self.assertRaises(NodeAPIError) as raised:
                    await node.hard_reset()
                self.assertEqual(raised.exception.code, 500)
                self.assertEqual(raised.exception.detail, self.command_body["detail"])
                self.assertEqual(await node.get_health(), Health.HEALTHY)
                lease = await coordinator.try_acquire(node.node_id, "other-worker", LifecycleOperation.START, 1)
                self.assertIsNotNone(lease)
                await coordinator.release(lease)

    async def test_maintenance_calls_outlive_default_timeout(self):
        node = self.make_node()
        calls = {
            "update_node": lambda: node.update_node(),
            "update_core": lambda: node.update_core({"core_version": "v25.8.31"}),
            "update_geofiles": lambda: node.update_geofiles({"region": "iran"}),
            "hard_reset": lambda: node.hard_reset(),
        }
        for name, call in calls.items():
            with self.subTest(name):
                response = await call()
                self.assertEqual(response.status_code, 200)

    async def test_maintenance_timeout_is_still_enforced(self):
        node = self.make_node()
        with (
            patch.dict(controller.MAINTENANCE_TIMEOUTS, {LifecycleOperation.UPDATE_NODE: 1}),
            self.assertRaises(NodeAPIError) as raised,
        ):
            await node.update_node()
        self.assertEqual(raised.exception.code, -5)

    async def test_lease_heartbeat_survives_a_coordinator_error(self):
        coordinator = FlakyHeartbeatCoordinator()
        node = self.make_node(lifecycle_coordinator=coordinator, lifecycle_lease_seconds=0.3)

        update = asyncio.create_task(node.update_node())
        await asyncio.sleep(0.8)  # well past the first lease's expiry, mid-call
        other = await coordinator.try_acquire(node.node_id, "other-worker", LifecycleOperation.START, 1)
        response = await update

        self.assertIsNone(other)
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(coordinator.heartbeats, 3)

    async def test_lost_lease_stops_renewal_but_not_the_call(self):
        coordinator = LostLeaseCoordinator()
        node = self.make_node(lifecycle_coordinator=coordinator, lifecycle_lease_seconds=0.3)

        with self.assertLogs("maintenance-test", logging.WARNING) as logs:
            response = await node.update_node()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(coordinator.heartbeats, 1)
        self.assertTrue(any("was lost" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
