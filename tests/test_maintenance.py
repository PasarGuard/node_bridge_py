"""Maintenance calls go to node-serviced, which runs pg-node commands that take minutes."""

import asyncio
import logging
import unittest
from unittest.mock import patch

from aiohttp import web

from PasarGuardNodeBridge import controller
from PasarGuardNodeBridge.aiohttp_compat import LazyClientSession, make_timeout
from PasarGuardNodeBridge.controller import NodeAPIError
from PasarGuardNodeBridge.rest import Node as RestNode
from PasarGuardNodeBridge.storage import InMemoryNodeLifecycleCoordinator, InMemoryUserSyncStore, LifecycleOperation

SLOW_COMMAND_SECONDS = 1.5


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


class MaintenanceTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async def root(request):
            return web.json_response({"status": "ok"})

        async def slow_command(request):
            await asyncio.sleep(SLOW_COMMAND_SECONDS)
            return web.json_response({"status": "ok"})

        app = web.Application()
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
        await self.runner.cleanup()

    def make_node(self, **kwargs):
        with patch("ssl.SSLContext.load_verify_locations"):
            node = RestNode(
                address="127.0.0.1",
                port=1,
                api_port=self.api_port,
                server_ca="test",
                api_key="00000000-0000-0000-0000-000000000000",
                node_id=f"maintenance-{len(self.nodes)}",
                user_sync_store=InMemoryUserSyncStore(),
                default_timeout=1,
                logger=logging.getLogger("maintenance-test"),
                **kwargs,
            )
        # Talk plain HTTP to the local stand-in for node-serviced.
        node._json_client = LazyClientSession(
            ssl_context=None, headers={}, base_url=f"http://127.0.0.1:{self.api_port}", timeout=make_timeout(30)
        )
        self.nodes.append(node)
        return node

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

        response = await node.update_node()

        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(coordinator.heartbeats, 3)


if __name__ == "__main__":
    unittest.main()
