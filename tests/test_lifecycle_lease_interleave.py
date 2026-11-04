import asyncio
import unittest

from PasarGuardNodeBridge.storage import LifecycleOperation
from tests.test_lifecycle_heartbeat import LifecycleHeartbeatTests, UnconfirmedWriteCoordinator


class LeaseInterleaveTests(LifecycleHeartbeatTests):
    async def test_two_workers_never_both_renew_one_lease(self):
        coordinator = UnconfirmedWriteCoordinator(failed_writes=2)
        first = self.make_node(coordinator, lease_seconds=0.6)
        second = self.make_node(coordinator, lease_seconds=0.6)
        second.node_id = first.node_id

        update = asyncio.create_task(first.update_node())
        await asyncio.sleep(0.05)
        rejected = 0
        for _ in range(12):
            await asyncio.sleep(0.1)
            try:
                await second._acquire_lifecycle_lease(LifecycleOperation.START)
            except Exception:
                rejected += 1
                continue
            self.fail("second worker acquired while the first still renewed")
        await update
        self.assertEqual(rejected, 12)


if __name__ == "__main__":
    unittest.main()
