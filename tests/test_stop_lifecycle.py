import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from PasarGuardNodeBridge.controller import Health, NodeAPIError
from PasarGuardNodeBridge.grpclib import Node as GrpcNode
from PasarGuardNodeBridge.rest import Node as RestNode
from PasarGuardNodeBridge.storage import (
    InMemoryNodeLifecycleCoordinator,
    InMemoryUserSyncStore,
    LifecycleOperation,
    LifecycleStatus,
)


class StopLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_during_coordinator_release_waits_for_cleanup(self):
        class SlowReleaseCoordinator(InMemoryNodeLifecycleCoordinator):
            def __init__(self):
                super().__init__()
                self.release_entered = asyncio.Event()
                self.finish_release = asyncio.Event()

            async def release(self, lease, state_update=None):
                self.release_entered.set()
                await self.finish_release.wait()
                await super().release(lease, state_update)

        coordinator = SlowReleaseCoordinator()
        node = RestNode.__new__(RestNode)
        self._configure_node(node, coordinator)
        lease = await coordinator.try_acquire(node.node_id, node.worker_id, LifecycleOperation.STOP, 30)
        self.assertIsNotNone(lease)
        release = asyncio.create_task(
            node._release_lifecycle_lease(
                lease,
                observed=LifecycleStatus.STOPPED,
                desired=LifecycleStatus.STOPPED,
            )
        )
        await asyncio.wait_for(coordinator.release_entered.wait(), timeout=1)
        release.cancel()
        await asyncio.sleep(0)
        self.assertFalse(release.done())

        coordinator.finish_release.set()
        with self.assertRaises(asyncio.CancelledError):
            await release
        state = await coordinator.get_state(node.node_id)
        self.assertEqual(state.observed, LifecycleStatus.STOPPED)
        self.assertIsNone(state.operation)

    def _configure_node(self, node, coordinator: InMemoryNodeLifecycleCoordinator) -> None:
        node.node_id = "node-1"
        node.name = "node-1"
        node.worker_id = "worker-1"
        node._default_timeout = 10
        node._node_lock = asyncio.Lock()
        node._lifecycle_coordinator = coordinator
        node._lifecycle_lease_seconds = 30
        node._lifecycle_heartbeat_tasks = {}
        node.logger = Mock()
        node.get_health = AsyncMock(return_value=Health.HEALTHY)
        node.disconnect = AsyncMock()
        node._json_client = SimpleNamespace(close=AsyncMock())

    async def _assert_failed_stop_keeps_lease(self, node) -> None:
        with self.assertRaises(NodeAPIError) as error:
            await node.stop()
        self.assertEqual(error.exception.code, 503)
        state = await node._lifecycle_coordinator.get_state(node.node_id)
        self.assertEqual(state.observed, LifecycleStatus.STOPPING)
        competing = await node._lifecycle_coordinator.try_acquire(
            node.node_id, "worker-2", LifecycleOperation.START, 30
        )
        self.assertIsNone(competing)
        self.assertEqual(node._lifecycle_heartbeat_tasks, {})
        node.disconnect.assert_not_awaited()

    async def test_rest_stop_propagates_error_without_releasing_lease(self):
        coordinator = InMemoryNodeLifecycleCoordinator()
        node = RestNode.__new__(RestNode)
        self._configure_node(node, coordinator)
        node._client = SimpleNamespace(close=AsyncMock())
        node._make_request = AsyncMock(side_effect=NodeAPIError(503, "REST stop failed"))
        await self._assert_failed_stop_keeps_lease(node)
        node._make_request.assert_awaited_once_with(method="PUT", endpoint="stop", timeout=10)

    async def test_grpc_stop_propagates_error_without_releasing_lease(self):
        coordinator = InMemoryNodeLifecycleCoordinator()
        node = GrpcNode.__new__(GrpcNode)
        self._configure_node(node, coordinator)
        node._client = SimpleNamespace(Stop=AsyncMock())
        node._handle_grpc_request = AsyncMock(side_effect=NodeAPIError(503, "gRPC stop failed"))
        await self._assert_failed_stop_keeps_lease(node)
        node._handle_grpc_request.assert_awaited_once()

    async def test_failed_heartbeat_does_not_mask_stop_error(self):
        coordinator = InMemoryNodeLifecycleCoordinator()
        node = RestNode.__new__(RestNode)
        self._configure_node(node, coordinator)
        node._client = SimpleNamespace(close=AsyncMock())
        node._make_request = AsyncMock(side_effect=NodeAPIError(503, "REST stop failed"))

        async def heartbeat_that_fails_during_cleanup():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as exc:
                raise RuntimeError("heartbeat failed") from exc

        lease = await coordinator.try_acquire(node.node_id, node.worker_id, LifecycleOperation.STOP, 30)
        self.assertIsNotNone(lease)
        node._acquire_lifecycle_lease = AsyncMock(return_value=lease)
        node._lifecycle_heartbeat_tasks[lease.token] = asyncio.create_task(heartbeat_that_fails_during_cleanup())
        await asyncio.sleep(0)
        with self.assertRaises(NodeAPIError) as error:
            await node.stop()
        self.assertEqual(error.exception.detail, "REST stop failed")
        node.logger.exception.assert_called_once()

    async def test_release_propagates_caller_cancellation_after_recording_state(self):
        coordinator = InMemoryNodeLifecycleCoordinator()
        node = RestNode.__new__(RestNode)
        self._configure_node(node, coordinator)
        lease = await coordinator.try_acquire(node.node_id, node.worker_id, LifecycleOperation.STOP, 30)
        self.assertIsNotNone(lease)
        heartbeat_cancelling = asyncio.Event()

        async def slow_heartbeat_shutdown():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                heartbeat_cancelling.set()
                await asyncio.Event().wait()

        node._lifecycle_heartbeat_tasks[lease.token] = asyncio.create_task(slow_heartbeat_shutdown())
        release = asyncio.create_task(
            node._release_lifecycle_lease(
                lease,
                observed=LifecycleStatus.STOPPED,
                desired=LifecycleStatus.STOPPED,
            )
        )
        await asyncio.wait_for(heartbeat_cancelling.wait(), timeout=1)
        release.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await release
        state = await coordinator.get_state(node.node_id)
        self.assertEqual(state.observed, LifecycleStatus.STOPPED)
        self.assertIsNone(state.operation)

    async def test_ambiguous_start_retains_lifecycle_lease(self):
        coordinator = InMemoryNodeLifecycleCoordinator()
        node = RestNode.__new__(RestNode)
        self._configure_node(node, coordinator)
        node._user_sync_store = InMemoryUserSyncStore()
        node._sync_lease_seconds = 30
        node._internal_timeout = 1
        node._user_sync_epoch_supported = True
        node._user_sync_epoch_capability_probed = True
        node._user_sync_epoch_handshake_lock = asyncio.Lock()
        node._user_sync_connection_generation = 0
        node._make_request = AsyncMock(side_effect=TimeoutError)

        with self.assertRaises(TimeoutError):
            await node.start(
                config="{}",
                backend_type=0,
                users=[],
                reconcile_user_sync=True,
            )

        competing = await coordinator.try_acquire("node-1", "worker-2", LifecycleOperation.STOP, 30)
        self.assertIsNone(competing)
        state = await coordinator.get_state("node-1")
        self.assertEqual(state.operation, LifecycleOperation.START)

    async def test_management_timeout_retains_lifecycle_lease(self):
        coordinator = InMemoryNodeLifecycleCoordinator()
        node = RestNode.__new__(RestNode)
        self._configure_node(node, coordinator)
        node.check_connectivity = AsyncMock(return_value=True)
        node._make_json_request = AsyncMock(side_effect=TimeoutError)

        with self.assertRaises(TimeoutError):
            await node._run_coordinated_update(
                LifecycleOperation.UPDATE_CORE,
                "/node/core_update",
                {"version": "latest"},
            )

        competing = await coordinator.try_acquire("node-1", "worker-2", LifecycleOperation.STOP, 30)
        self.assertIsNone(competing)

    async def test_successful_start_with_lost_heartbeat_releases_known_outcome(self):
        class LostHeartbeatCoordinator(InMemoryNodeLifecycleCoordinator):
            async def heartbeat(self, _lease):
                return False

        coordinator = LostHeartbeatCoordinator()
        node = RestNode.__new__(RestNode)
        self._configure_node(node, coordinator)
        node._lifecycle_lease_seconds = 0.001
        node._user_sync_store = InMemoryUserSyncStore()
        node._sync_lease_seconds = 30
        node._internal_timeout = 1
        node._user_sync_epoch_supported = True
        node._user_sync_epoch_capability_probed = True
        node._user_sync_epoch_handshake_lock = asyncio.Lock()
        node._user_sync_connection_generation = 0

        async def successful_start(**_kwargs):
            await asyncio.sleep(0.02)
            return SimpleNamespace(
                started=True,
                node_version="0.4.0",
                core_version="1.0.0",
                user_sync_epoch_supported=True,
                user_sync_epoch=1,
            )

        node._make_request = successful_start
        node.connect = AsyncMock()

        response = await node.start(
            config="{}",
            backend_type=0,
            users=[],
            reconcile_user_sync=True,
        )

        self.assertTrue(response.started)
        state = await coordinator.get_state("node-1")
        self.assertEqual(state.observed, LifecycleStatus.HEALTHY)
        competing = await coordinator.try_acquire("node-1", "worker-2", LifecycleOperation.STOP, 30)
        self.assertIsNotNone(competing)
        node.logger.error.assert_called_once()

    async def test_hard_reset_timeout_retains_unknown_lifecycle_lease(self):
        coordinator = InMemoryNodeLifecycleCoordinator()
        node = RestNode.__new__(RestNode)
        self._configure_node(node, coordinator)
        node.check_connectivity = AsyncMock(return_value=True)
        node._make_json_request = AsyncMock(side_effect=TimeoutError)

        with self.assertRaises(TimeoutError):
            await node.hard_reset()

        competing = await coordinator.try_acquire("node-1", "worker-2", LifecycleOperation.START, 30)
        self.assertIsNone(competing)
        node.disconnect.assert_not_awaited()

    async def test_lifecycle_adapter_errors_are_translated(self):
        class FailingCoordinator(InMemoryNodeLifecycleCoordinator):
            async def try_acquire(self, *_args, **_kwargs):
                raise RuntimeError("shared store unavailable")

        node = RestNode.__new__(RestNode)
        self._configure_node(node, FailingCoordinator())

        with self.assertRaises(NodeAPIError) as error:
            await node._acquire_lifecycle_lease(LifecycleOperation.START)

        self.assertEqual(error.exception.code, 503)
        self.assertEqual(error.exception.detail, "Lifecycle coordinator acquire failed")
        self.assertNotIn("shared store unavailable", error.exception.detail)


if __name__ == "__main__":
    unittest.main()
