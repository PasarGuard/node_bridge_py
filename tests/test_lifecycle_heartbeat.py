import asyncio
import logging
import unittest
from unittest.mock import patch

from aiohttp import web

from PasarGuardNodeBridge.aiohttp_compat import LazyClientSession, make_timeout
from PasarGuardNodeBridge.rest import Node as RestNode
from PasarGuardNodeBridge.storage import InMemoryNodeLifecycleCoordinator, InMemoryUserSyncStore, LifecycleOperation

COMMAND_SECONDS = 1.6
LOGGER_NAME = "lifecycle-heartbeat-test"


class UnconfirmedWriteCoordinator(InMemoryNodeLifecycleCoordinator):
    def __init__(self, failed_writes):
        super().__init__()
        self.failed_writes = failed_writes
        self.heartbeats = 0

    async def heartbeat(self, lease):
        self.heartbeats += 1
        async with self._lock:
            current = self._leases.get(lease.node_id)
            if current is None or current[0].token != lease.token:
                return False
        if self.failed_writes is None or self.heartbeats <= self.failed_writes:
            return False
        return await super().heartbeat(lease)


class LifecycleHeartbeatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async def root(request):
            return web.json_response({"status": "ok"})

        async def command(request):
            await asyncio.sleep(COMMAND_SECONDS)
            return web.json_response({"status": "ok"})

        app = web.Application()
        app.router.add_get("/", root)
        app.router.add_post("/node/update", command)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", 0).start()
        self.api_port = self.runner.addresses[0][1]
        self.nodes = []

    async def asyncTearDown(self):
        for node in self.nodes:
            await node._json_client.close()
        await self.runner.cleanup()

    def make_node(self, coordinator, lease_seconds):
        with patch("ssl.SSLContext.load_verify_locations"):
            node = RestNode(
                address="127.0.0.1",
                port=1,
                api_port=self.api_port,
                server_ca="test",
                api_key="00000000-0000-0000-0000-000000000000",
                node_id=f"heartbeat-{len(self.nodes)}",
                user_sync_store=InMemoryUserSyncStore(),
                default_timeout=10,
                logger=logging.getLogger(LOGGER_NAME),
                lifecycle_coordinator=coordinator,
                lifecycle_lease_seconds=lease_seconds,
            )
        node._json_client = LazyClientSession(
            ssl_context=None, headers={}, base_url=f"http://127.0.0.1:{self.api_port}", timeout=make_timeout(30)
        )
        self.nodes.append(node)
        return node

    async def test_an_unconfirmed_renewal_while_the_lease_is_still_held_keeps_renewing(self):
        coordinator = UnconfirmedWriteCoordinator(failed_writes=1)
        node = self.make_node(coordinator, lease_seconds=0.6)

        with self.assertLogs(LOGGER_NAME, logging.WARNING) as logs:
            update = asyncio.create_task(node.update_node())
            await asyncio.sleep(1.1)
            other = await coordinator.try_acquire(node.node_id, "other-worker", LifecycleOperation.START, 1)
            response = await update

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(other, f"another worker took the lease after {coordinator.heartbeats} heartbeat(s)")
        self.assertGreaterEqual(coordinator.heartbeats, 4)
        self.assertFalse(any("was lost" in line for line in logs.output), logs.output)


if __name__ == "__main__":
    unittest.main()
