"""Maintenance calls go to node-serviced, which runs pg-node commands that take minutes."""

import asyncio
import logging
import unittest
from unittest.mock import patch

from aiohttp import web

from PasarGuardNodeBridge.aiohttp_compat import LazyClientSession, make_timeout
from PasarGuardNodeBridge.rest import Node as RestNode
from PasarGuardNodeBridge.storage import InMemoryUserSyncStore

SLOW_COMMAND_SECONDS = 1.5


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
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]

        with patch("ssl.SSLContext.load_verify_locations"):
            self.node = RestNode(
                address="127.0.0.1",
                port=1,
                api_port=port,
                server_ca="test",
                api_key="00000000-0000-0000-0000-000000000000",
                node_id="maintenance",
                user_sync_store=InMemoryUserSyncStore(),
                default_timeout=1,
                logger=logging.getLogger("maintenance-test"),
            )
        # Talk plain HTTP to the local stand-in for node-serviced.
        await self.node._json_client.close()
        self.node._json_client = LazyClientSession(
            ssl_context=None, headers={}, base_url=f"http://127.0.0.1:{port}", timeout=make_timeout(30)
        )

    async def asyncTearDown(self):
        await self.node._json_client.close()
        await self.runner.cleanup()

    async def test_maintenance_calls_outlive_default_timeout(self):
        calls = {
            "update_node": lambda: self.node.update_node(),
            "update_core": lambda: self.node.update_core({"core_version": "v25.8.31"}),
            "update_geofiles": lambda: self.node.update_geofiles({"region": "iran"}),
            "hard_reset": lambda: self.node.hard_reset(),
        }
        for name, call in calls.items():
            with self.subTest(name):
                response = await call()
                self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
