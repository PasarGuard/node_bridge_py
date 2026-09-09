import asyncio
import time
import unittest
from typing import Any, cast
from unittest.mock import AsyncMock, patch

from PasarGuardNodeBridge.common.service_pb2 import User
from PasarGuardNodeBridge.controller import Controller, NodeAPIError
from PasarGuardNodeBridge.storage import (
    ClaimedUser,
    InMemoryNodeLifecycleCoordinator,
    InMemoryNodeRegistry,
    InMemoryUserSyncStore,
    LifecycleLease,
    LifecycleLeaseLostError,
    LifecycleOperation,
    LifecycleStatus,
    NodeConfig,
    NodeLifecycleState,
    UserSyncStoreFullError,
)


class InMemoryUserSyncStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_execution_epochs_are_monotonic_and_survive_narrowing(self):
        store = InMemoryUserSyncStore()
        first = await store.acquire_user_sync_lease("node-1", "worker-1", ["a@example.com", "b@example.com"], 30)
        narrowed = await store.retain_user_sync_lease_keys(first, ["a@example.com"])
        self.assertEqual(narrowed.epoch, first.epoch)
        await store.release_user_sync_lease(narrowed)

        second = (await store.acquire_startup_user_sync_lease("node-1", "worker-2", ["a@example.com"], 30)).lease
        self.assertGreater(second.epoch, first.epoch)
        await store.release_user_sync_lease(second)

        other_node = await store.acquire_user_sync_lease("node-2", "worker-3", ["a@example.com"], 30)
        self.assertEqual(other_node.epoch, 1)
        await store.release_user_sync_lease(other_node)

    async def test_snapshot_reads_do_not_allocate_revocation_state_for_unseen_users(self):
        store = InMemoryUserSyncStore()

        startup = await store.acquire_startup_user_sync_lease(
            "node-1", "worker-1", [f"user-{index}@example.com" for index in range(100)], 30
        )

        self.assertNotIn("node-1", store._revocations)
        await store.release_user_sync_lease(startup.lease)

        recovery = await store.acquire_user_sync_reconciliation_lease(
            "node-1", "worker-2", [f"other-{index}@example.com" for index in range(100)], 30
        )

        self.assertNotIn("node-1", store._revocations)
        await store.release_user_sync_lease(recovery.lease)

    async def test_enqueue_claim_ack_removes_user(self):
        store = InMemoryUserSyncStore()
        await store.enqueue_users("node-1", [User(email="a@example.com")])

        claimed = await store.claim_users("node-1", "worker-1", limit=10, lease_seconds=30)
        self.assertEqual([item.user.email for item in claimed], ["a@example.com"])

        await store.ack_users("node-1", [item.token for item in claimed])
        self.assertEqual(await store.claim_users("node-1", "worker-1", limit=10, lease_seconds=30), [])

    async def test_latest_email_wins_before_claim(self):
        store = InMemoryUserSyncStore()
        old = User(email="a@example.com", inbounds=["old"])
        new = User(email="a@example.com", inbounds=["new"])

        await store.enqueue_users("node-1", [old])
        await store.enqueue_users("node-1", [new])
        claimed = await store.claim_users("node-1", "worker-1", limit=10, lease_seconds=30)

        self.assertEqual(len(claimed), 1)
        self.assertEqual(list(claimed[0].user.inbounds), ["new"])

    async def test_claims_are_exclusive_until_requeue_or_lease_expiry(self):
        store = InMemoryUserSyncStore()
        await store.enqueue_users("node-1", [User(email="a@example.com"), User(email="b@example.com")])

        first = await store.claim_users("node-1", "worker-1", limit=1, lease_seconds=30)
        second = await store.claim_users("node-1", "worker-2", limit=10, lease_seconds=30)

        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertNotEqual(first[0].user.email, second[0].user.email)

    async def test_requeue_makes_failed_claim_available_again(self):
        store = InMemoryUserSyncStore()
        await store.enqueue_users("node-1", [User(email="a@example.com")])
        claimed = await store.claim_users("node-1", "worker-1", limit=10, lease_seconds=30)

        await store.requeue_users("node-1", claimed)
        claimed_again = await store.claim_users("node-1", "worker-2", limit=10, lease_seconds=30)

        self.assertEqual([item.user.email for item in claimed_again], ["a@example.com"])

    async def test_requeue_ignores_acknowledged_or_unknown_tokens(self):
        store = InMemoryUserSyncStore()
        await store.enqueue_users("node-1", [User(email="acked@example.com")])
        claimed = await store.claim_users("node-1", "worker-1", limit=10, lease_seconds=30)
        await store.ack_users("node-1", [claimed[0].token])

        await store.requeue_users(
            "node-1",
            [claimed[0], ClaimedUser(token="unknown", user=User(email="injected@example.com"))],
        )

        self.assertEqual(await store.claim_users("node-1", "worker-2", limit=10, lease_seconds=30), [])

    async def test_expired_lease_becomes_claimable(self):
        store = InMemoryUserSyncStore()
        await store.enqueue_users("node-1", [User(email="a@example.com")])
        await store.claim_users("node-1", "worker-1", limit=10, lease_seconds=0)

        claimed_again = await store.claim_users("node-1", "worker-2", limit=10, lease_seconds=30)

        self.assertEqual([item.user.email for item in claimed_again], ["a@example.com"])

    async def test_next_claim_delay_distinguishes_empty_pending_and_leased_work(self):
        store = InMemoryUserSyncStore()

        self.assertIsNone(await store.next_claim_delay("node-1"))

        await store.enqueue_users("node-1", [User(email="a@example.com")])
        self.assertEqual(await store.next_claim_delay("node-1"), 0.0)

        with patch("PasarGuardNodeBridge.storage.time.monotonic", side_effect=[100.0, 100.025]):
            await store.claim_users("node-1", "worker-1", limit=10, lease_seconds=0.1)
            delay = await store.next_claim_delay("node-1")
        self.assertIsNotNone(delay)
        self.assertAlmostEqual(delay, 0.075)

    async def test_enqueue_rejects_work_above_per_node_bound_without_partial_write(self):
        store = InMemoryUserSyncStore(max_pending_users_per_node=1)
        await store.enqueue_users("node-1", [User(email="a@example.com")])

        with self.assertRaises(UserSyncStoreFullError):
            await store.enqueue_users("node-1", [User(email="a@example.com"), User(email="b@example.com")])

        claimed = await store.claim_users("node-1", "worker-1", limit=10, lease_seconds=30)
        self.assertEqual([item.user.email for item in claimed], ["a@example.com"])

    async def test_claimed_users_count_toward_per_node_bound(self):
        store = InMemoryUserSyncStore(max_pending_users_per_node=1)
        await store.enqueue_users("node-1", [User(email="a@example.com")])
        await store.claim_users("node-1", "worker-1", limit=10, lease_seconds=30)

        with self.assertRaises(UserSyncStoreFullError):
            await store.enqueue_users("node-1", [User(email="b@example.com")])

    def test_non_positive_per_node_bound_is_rejected(self):
        with self.assertRaises(ValueError):
            InMemoryUserSyncStore(max_pending_users_per_node=0)


class InMemoryNodeRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_registry_roundtrip(self):
        registry = InMemoryNodeRegistry()
        config = NodeConfig(
            connection="grpc",
            address="127.0.0.1",
            port=2096,
            api_port=2097,
            server_ca="cert",
            api_key="00000000-0000-0000-0000-000000000000",
        )

        await registry.upsert_node("node-1", config)

        self.assertEqual(await registry.get_node("node-1"), config)
        self.assertEqual(await registry.list_nodes(), ["node-1"])
        await registry.delete_node("node-1")
        self.assertIsNone(await registry.get_node("node-1"))


class InMemoryNodeLifecycleCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_state_uses_wall_clock_timestamp(self):
        coordinator = InMemoryNodeLifecycleCoordinator()
        before = time.time()

        lease = await coordinator.try_acquire("node-1", "worker-1", LifecycleOperation.START, 30)
        state = await coordinator.get_state("node-1")

        self.assertIsNotNone(lease)
        self.assertGreaterEqual(state.updated_at, before)
        self.assertLessEqual(state.updated_at, time.time())

    async def test_lifecycle_lease_is_exclusive(self):
        coordinator = InMemoryNodeLifecycleCoordinator()

        first = await coordinator.try_acquire("node-1", "worker-1", LifecycleOperation.RECONNECT, 30)
        second = await coordinator.try_acquire("node-1", "worker-2", LifecycleOperation.RECONNECT, 30)

        self.assertIsNotNone(first)
        self.assertIsNone(second)

    async def test_release_records_final_state(self):
        coordinator = InMemoryNodeLifecycleCoordinator()
        lease = await coordinator.try_acquire("node-1", "worker-1", LifecycleOperation.START, 30)
        self.assertIsNotNone(lease)

        await coordinator.release(
            lease,
            state_update=NodeLifecycleState(
                desired=LifecycleStatus.HEALTHY,
                observed=LifecycleStatus.HEALTHY,
                epoch=lease.epoch,
                node_version="0.2.0",
                core_version="1.0.0",
            ),
        )
        state = await coordinator.get_state("node-1")

        self.assertEqual(state.observed, LifecycleStatus.HEALTHY)
        self.assertEqual(state.operation, None)
        self.assertEqual(state.owner, None)
        self.assertEqual(state.node_version, "0.2.0")

    async def test_expired_lease_requires_reconciliation_before_new_operation(self):
        coordinator = InMemoryNodeLifecycleCoordinator()
        first = await coordinator.try_acquire("node-1", "worker-1", LifecycleOperation.START, 0.001)
        await asyncio.sleep(0.01)
        second = await coordinator.try_acquire("node-1", "worker-2", LifecycleOperation.STOP, 30)
        self.assertIsNotNone(first)
        self.assertIsNone(second)

        self.assertTrue(await coordinator.reconcile("node-1", LifecycleStatus.HEALTHY))
        second = await coordinator.try_acquire("node-1", "worker-2", LifecycleOperation.STOP, 30)
        self.assertIsNotNone(second)

        await coordinator.update_observed("node-1", LifecycleStatus.BROKEN, expected_epoch=first.epoch)
        state = await coordinator.get_state("node-1")

        self.assertEqual(state.epoch, second.epoch)
        self.assertNotEqual(state.observed, LifecycleStatus.BROKEN)

    async def test_active_lifecycle_lease_cannot_be_reconciled(self):
        coordinator = InMemoryNodeLifecycleCoordinator()
        await coordinator.try_acquire("node-1", "worker-1", LifecycleOperation.RECONNECT, 30)

        self.assertFalse(await coordinator.reconcile("node-1", LifecycleStatus.HEALTHY))
        self.assertIsNone(await coordinator.try_acquire("node-1", "worker-2", LifecycleOperation.RECONNECT, 30))

    async def test_controller_detects_lifecycle_heartbeat_ownership_loss(self):
        controller = Controller.__new__(Controller)
        controller.node_id = "node-1"
        controller._lifecycle_lease_seconds = 0.001
        controller._lifecycle_coordinator = cast(
            Any,
            type("LostCoordinator", (), {"heartbeat": AsyncMock(return_value=False)})(),
        )
        lease = LifecycleLease(
            node_id="node-1",
            worker_id="worker-1",
            operation=LifecycleOperation.START,
            token="lost-token",
            epoch=1,
            lease_seconds=0.001,
        )

        with self.assertRaises(LifecycleLeaseLostError):
            await controller._heartbeat_lifecycle_lease(lease)

    async def test_node_update_is_exclusive_across_controllers(self):
        coordinator = InMemoryNodeLifecycleCoordinator()
        request_started = asyncio.Event()
        finish_request = asyncio.Event()
        response = object()

        async def blocking_request(**kwargs):
            request_started.set()
            await finish_request.wait()
            return response

        first = Controller.__new__(Controller)
        first.node_id = "node-1"
        first.worker_id = "worker-1"
        first._lifecycle_coordinator = coordinator
        first._lifecycle_lease_seconds = 30
        first._lifecycle_heartbeat_tasks = {}
        first.check_connectivity = AsyncMock(return_value=True)
        first._make_json_request = cast(Any, blocking_request)

        second = Controller.__new__(Controller)
        second.node_id = "node-1"
        second.worker_id = "worker-2"
        second._lifecycle_coordinator = coordinator
        second._lifecycle_lease_seconds = 30
        second._lifecycle_heartbeat_tasks = {}
        second.check_connectivity = AsyncMock(return_value=True)
        second._make_json_request = AsyncMock()

        first_update = asyncio.create_task(first.update_node())
        await request_started.wait()
        try:
            with self.assertRaises(NodeAPIError) as error:
                await second.update_node()
            self.assertEqual(error.exception.code, 409)
            second._make_json_request.assert_not_awaited()
        finally:
            finish_request.set()

        self.assertIs(await first_update, response)


if __name__ == "__main__":
    unittest.main()
