import asyncio
import unittest

from PasarGuardNodeBridge.common.service_pb2 import User
from PasarGuardNodeBridge.storage import (
    InMemoryNodeLifecycleCoordinator,
    InMemoryNodeRegistry,
    InMemoryUserSyncStore,
    LifecycleOperation,
    LifecycleStatus,
    NodeConfig,
    NodeLifecycleState,
)


class InMemoryUserSyncStoreTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_expired_lease_becomes_claimable(self):
        store = InMemoryUserSyncStore()
        await store.enqueue_users("node-1", [User(email="a@example.com")])
        await store.claim_users("node-1", "worker-1", limit=10, lease_seconds=0.001)
        await asyncio.sleep(0.01)

        claimed_again = await store.claim_users("node-1", "worker-2", limit=10, lease_seconds=30)

        self.assertEqual([item.user.email for item in claimed_again], ["a@example.com"])


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

    async def test_stale_observed_update_is_ignored(self):
        coordinator = InMemoryNodeLifecycleCoordinator()
        first = await coordinator.try_acquire("node-1", "worker-1", LifecycleOperation.START, 0.001)
        await asyncio.sleep(0.01)
        second = await coordinator.try_acquire("node-1", "worker-2", LifecycleOperation.STOP, 30)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)

        await coordinator.update_observed("node-1", LifecycleStatus.BROKEN, expected_epoch=first.epoch)
        state = await coordinator.get_state("node-1")

        self.assertEqual(state.epoch, second.epoch)
        self.assertNotEqual(state.observed, LifecycleStatus.BROKEN)

    async def test_expired_lifecycle_lease_can_be_reacquired(self):
        coordinator = InMemoryNodeLifecycleCoordinator()
        await coordinator.try_acquire("node-1", "worker-1", LifecycleOperation.RECONNECT, 0)
        await asyncio.sleep(0.01)

        lease = await coordinator.try_acquire("node-1", "worker-2", LifecycleOperation.RECONNECT, 30)

        self.assertIsNotNone(lease)
        self.assertEqual(lease.worker_id, "worker-2")


if __name__ == "__main__":
    unittest.main()
