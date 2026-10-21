import asyncio
import logging
import unittest
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

from PasarGuardNodeBridge.common.service_pb2 import Empty, User
from PasarGuardNodeBridge.controller import Controller, FailedUsers, NodeAPIError
from PasarGuardNodeBridge.storage import InMemoryUserSyncStore


async def eventually(predicate, timeout=4):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


class RecordingNode(Controller):
    def __init__(self, **kwargs):
        with patch("ssl.SSLContext.load_verify_locations"):
            super().__init__(
                server_ca="test",
                api_key="00000000-0000-0000-0000-000000000000",
                service_url="http://localhost/",
                node_id="node",
                logger=logging.getLogger("sync-test"),
                sync_poll_interval=0.01,
                **kwargs,
            )
        self.delivered = []
        self.chunked_batches = []
        self.legacy_batches = []
        self.snapshots = []

    async def sync_users_chunked(self, users, **kwargs):
        self.chunked_batches.append((users, kwargs))
        self.delivered.extend(users)
        return []

    async def _sync_batch_users(self, users):
        self.legacy_batches.append(users)
        self.delivered.extend(users)
        return []

    async def sync_users(self, users, **kwargs):
        self.snapshots.append(users)
        return Empty()


class QueueStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = InMemoryUserSyncStore()
        self.old = User(email="a", inbounds=["old"])
        self.new = User(email="a", inbounds=["new"])

    async def claim(self, lease=30):
        return await self.store.claim_users("node", "worker", 100, lease)

    async def test_new_update_waits_for_active_delivery_and_survives_old_ack(self):
        await self.store.enqueue_users("node", [self.old])
        old = await self.claim()
        await self.store.enqueue_users("node", [self.new])
        self.assertEqual(await self.claim(), [])
        await self.store.ack_users("node", [old[0].token])
        self.assertEqual((await self.claim())[0].user, self.new)

    async def test_stale_requeue_cannot_resurrect_acknowledged_user(self):
        await self.store.enqueue_users("node", [self.old])
        old = await self.claim(0)
        await self.store.enqueue_users("node", [self.new])
        new = await self.claim()
        await self.store.ack_users("node", [new[0].token])
        await self.store.requeue_users("node", old)
        self.assertEqual(await self.claim(), [])

    async def test_stale_ack_and_requeue_cannot_release_new_owner(self):
        await self.store.enqueue_users("node", [self.old])
        old = await self.claim(0)
        new = await self.claim()
        await self.store.ack_users("node", [old[0].token])
        await self.store.requeue_users("node", old)
        self.assertEqual(await self.claim(), [])
        await self.store.ack_users("node", [new[0].token])
        self.assertFalse(await self.store.has_pending("node"))

    async def test_requeue_uses_latest_store_payload_not_mutated_claim(self):
        await self.store.enqueue_users("node", [self.old])
        claim = await self.claim()
        claim[0].user.inbounds[:] = ["mutated"]
        await self.store.requeue_users("node", claim)
        self.assertEqual((await self.claim())[0].user, self.old)

    async def test_enqueue_copies_mutable_protobuf(self):
        await self.store.enqueue_users("node", [self.old])
        self.old.inbounds[:] = ["mutated"]
        self.assertEqual(list((await self.claim())[0].user.inbounds), ["old"])

    async def test_snapshot_retirement_preserves_concurrent_updates(self):
        await self.store.enqueue_users("node", [self.old])
        captured, active = await self.store.capture_queued("node")
        self.assertEqual(active, 0)
        await self.store.enqueue_users("node", [self.new])
        self.assertEqual(await self.store.retire_captured("node", captured), 0)
        self.assertEqual((await self.claim())[0].user, self.new)

    async def test_clear_invalidates_old_tokens_without_revision_reuse(self):
        await self.store.enqueue_users("node", [self.old])
        captured, _ = await self.store.capture_queued("node")
        old = await self.claim()
        await self.store.clear("node")
        await self.store.enqueue_users("node", [self.new])
        await self.store.requeue_users("node", old)
        await self.store.retire_captured("node", captured)
        self.assertEqual((await self.claim())[0].user, self.new)

    async def test_stale_fence_owner_cannot_release_or_renew_replacement(self):
        old = await self.store.begin_full_sync("node", "old", 0)
        new = await self.store.begin_full_sync("node", "new", 30)
        await self.store.end_full_sync("node", old)
        self.assertFalse(await self.store.renew_full_sync("node", old, 30))
        self.assertEqual(await self.store.fence_state("node"), (True, 2))
        await self.store.end_full_sync("node", new)


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.nodes = []

    async def asyncTearDown(self):
        for node in self.nodes:
            await node.disconnect()
            await node._json_client.close()

    def node(self, store=None, **kwargs):
        node = RecordingNode(user_sync_store=store or InMemoryUserSyncStore(), **kwargs)
        self.nodes.append(node)
        return node

    async def test_empty_claim_does_not_erase_concurrent_enqueue_wake(self):
        store = InMemoryUserSyncStore()
        node = self.node(store)
        entered, release = asyncio.Event(), asyncio.Event()
        original = store.claim_users

        async def empty_read(*args, **kwargs):
            entered.set()
            await release.wait()
            return []

        store.claim_users = empty_read
        node._ensure_sync_worker_running = AsyncMock()
        claim = asyncio.create_task(node._claim_pending_users())
        await entered.wait()
        await node.update_user(User(email="a"))
        release.set()
        self.assertEqual(await claim, [])
        self.assertTrue(node._work_available.is_set())
        store.claim_users = original

    async def test_idle_timeout_with_concurrent_wake_keeps_draining(self):
        node = self.node()
        original_wait = node._work_available.wait
        calls = 0

        async def racing_wait():
            nonlocal calls
            calls += 1
            if calls == 1:
                await node.update_user(User(email="a"))
                raise TimeoutError
            return await original_wait()

        node._work_available.wait = racing_wait
        await node.connect("0.2.0", "1.0.0")
        await eventually(lambda: len(node.delivered) == 1)

    async def test_small_updates_use_chunked_and_large_queue_is_bounded(self):
        node = self.node(sync_batch_size=17, sync_chunk_size=7)
        await node.update_users([User(email=str(i)) for i in range(53)])
        await node.connect("0.2.0", "1.0.0")
        await eventually(lambda: len(node.delivered) == 53)
        self.assertEqual([len(batch) for batch, _ in node.chunked_batches], [17, 17, 17, 2])
        self.assertTrue(all(options["chunk_size"] == 7 for _, options in node.chunked_batches))
        self.assertEqual(node.legacy_batches, [])

    async def test_old_nodes_keep_legacy_transport(self):
        node = self.node()
        await node.update_user(User(email="a"))
        await node.connect("0.1.9", "1.0.0")
        await eventually(lambda: len(node.delivered) == 1)
        self.assertEqual(len(node.legacy_batches), 1)
        self.assertEqual(node.chunked_batches, [])

    async def test_disconnect_preserves_queue_and_connect_resumes_it(self):
        node = self.node()
        await node.update_user(User(email="a"))
        await node.disconnect()
        self.assertTrue(await node._user_sync_store.has_pending("node"))
        await node.connect("0.2.0", "1.0.0")
        await eventually(lambda: len(node.delivered) == 1)

    async def test_chunked_exception_retries_without_another_enqueue(self):
        node = self.node()
        original = node.sync_users_chunked
        node.sync_users_chunked = AsyncMock(side_effect=[OSError("unavailable"), []])
        await node.update_user(User(email="a"))
        await node.connect("0.2.0", "1.0.0")
        await eventually(lambda: node.sync_users_chunked.await_count == 2)
        await eventually(lambda: node._user_sync_failure_count == 0)
        self.assertFalse(await node._user_sync_store.has_pending("node"))
        node.sync_users_chunked = original

    async def test_claim_failure_retries_with_backoff(self):
        store = InMemoryUserSyncStore()
        node = self.node(store)
        claim = store.claim_users
        attempts = []

        async def fail_once(*args, **kwargs):
            attempts.append(asyncio.get_running_loop().time())
            if len(attempts) == 1:
                raise OSError("store unavailable")
            return await claim(*args, **kwargs)

        store.claim_users = fail_once
        await node.update_user(User(email="a"))
        await node.connect("0.2.0", "1.0.0")
        await eventually(lambda: len(node.delivered) == 1)
        self.assertGreaterEqual(attempts[1] - attempts[0], 0.9)

    async def test_ack_and_requeue_outage_recovers_after_lease_expiry(self):
        store = InMemoryUserSyncStore()
        node = self.node(store, sync_lease_seconds=0.2)
        ack, requeue = store.ack_users, store.requeue_users
        store.ack_users = AsyncMock(side_effect=OSError("ack offline"))
        store.requeue_users = AsyncMock(side_effect=OSError("requeue offline"))
        await node.update_user(User(email="a"))
        await node.connect("0.2.0", "1.0.0")
        await eventually(lambda: store.requeue_users.await_count == 1)
        store.ack_users, store.requeue_users = ack, requeue
        await eventually(lambda: len(node.delivered) == 2)
        await eventually(lambda: node._user_sync_failure_count == 0)
        self.assertFalse(await store.has_pending("node"))

    async def test_partial_failure_retries_only_failed_users(self):
        node = self.node()
        users = [User(email="a"), User(email="b")]
        node.sync_users_chunked = AsyncMock(side_effect=[[users[1]], []])
        await node.update_users(users)
        await node.connect("0.2.0", "1.0.0")
        await eventually(lambda: node.sync_users_chunked.await_count == 2)
        self.assertEqual(node.sync_users_chunked.call_args.args[0], [users[1]])

    def refuse(self, node, refused):
        """Fail any batch holding a user for which refused(user) is true, as the node's backend does."""
        sends = []

        async def send(users, **kwargs):
            sends.append([user.email for user in users])
            if any(refused(user) for user in users):
                return FailedUsers(users, rejected=True, detail="proxy is not a UserManager")
            node.delivered.extend(users)
            return []

        node.sync_users_chunked = send
        return sends

    @contextmanager
    def fast_backoff(self):
        sleep = asyncio.sleep

        async def short_sleep(delay, result=None):
            return await sleep(min(delay, 0.01), result)

        with patch("asyncio.sleep", short_sleep):
            yield

    def fast_retries(self, node, window=600.0):
        node._sync_retry_delays = (0.01, 0.02)
        node._sync_retry_window = window

    async def queue_drained(self, node):
        async with asyncio.timeout(4):
            while await node._user_sync_store.has_pending("node"):
                await asyncio.sleep(0.005)

    async def test_rejected_users_leave_the_queue_and_the_rest_is_delivered_in_one_round(self):
        node = self.node()
        self.refuse(node, lambda user: user.email in {"2", "7"})
        await node.update_users([User(email=str(i), inbounds=["ss"]) for i in range(10)])
        await node.connect("0.2.0", "1.0.0")
        await eventually(lambda: len(node.delivered) == 8)
        await self.queue_drained(node)
        self.assertEqual(set(node._sync_retries), {"2", "7"})
        self.assertEqual(node._user_sync_failure_count, 0)

    async def test_many_rejected_users_do_not_stall_the_queue(self):
        node = self.node()
        self.refuse(node, lambda user: "ss" in user.inbounds)
        await node.update_users([User(email=f"bad-{i}", inbounds=["ss"]) for i in range(150)])
        await node.update_users([User(email=f"ok-{i}", inbounds=["vless"]) for i in range(20)])
        await node.connect("0.2.0", "1.0.0")
        await eventually(lambda: len(node.delivered) == 20)
        await self.queue_drained(node)
        self.assertEqual(len(node._sync_retries), 150)

    async def test_transiently_rejected_user_is_retried_aside_and_delivered(self):
        node = self.node()
        self.fast_retries(node)
        rejections = 3

        def refused(user):
            nonlocal rejections
            if user.email == "a" and rejections:
                rejections -= 1
                return True
            return False

        sends = self.refuse(node, refused)
        await node.update_users([User(email="a", inbounds=["vless"]), User(email="b", inbounds=["vless"])])
        await node.connect("0.2.0", "1.0.0")
        await eventually(lambda: [user.email for user in node.delivered] == ["b", "a"])
        await eventually(lambda: not node._sync_retries)
        self.assertEqual(sends.count(["a"]), 3)

    async def test_rejected_add_is_dropped_after_the_retry_window(self):
        node = self.node()
        self.fast_retries(node, window=0.1)
        sends = self.refuse(node, lambda user: user.email == "a")
        with self.assertLogs("sync-test", logging.ERROR) as logs:
            await node.update_user(User(email="a", inbounds=["ss"]))
            await node.connect("0.2.0", "1.0.0")
            await eventually(lambda: sends and not node._sync_retries)
        self.assertEqual(node.delivered, [])
        self.assertGreater(sends.count(["a"]), 2)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("user a", logs.output[0])
        self.assertIn("dropping this update", logs.output[0])
        self.assertIn("proxy is not a UserManager", logs.output[0])
        self.assertFalse(await node._user_sync_store.has_pending("node"))

    async def test_rejected_removal_is_never_dropped(self):
        node = self.node()
        self.fast_retries(node, window=0.05)
        sends = self.refuse(node, lambda user: user.email == "gone")
        await node.update_user(User(email="gone"))
        await node.connect("0.2.0", "1.0.0")
        await eventually(lambda: sends.count(["gone"]) > 8)
        self.assertIn("gone", node._sync_retries)
        await node.update_user(User(email="later", inbounds=["vless"]))
        await eventually(lambda: [user.email for user in node.delivered] == ["later"])
        await node.flush_pending_users()
        self.assertEqual(node._sync_retries, {})

    async def test_newer_change_discards_the_rejected_payload(self):
        store = InMemoryUserSyncStore()
        node = self.node(store)
        self.fast_retries(node)
        self.refuse(node, lambda user: "ss" in user.inbounds)
        refusing = node.sync_users_chunked

        async def send(users, **kwargs):
            if [(user.email, list(user.inbounds)) for user in users] == [("a", ["ss"])] and not node.delivered:
                # Changed again while the rejected revision is in flight.
                await node.update_user(User(email="a", inbounds=["vless"]))
            return await refusing(users, **kwargs)

        node.sync_users_chunked = send
        await node.update_user(User(email="a", inbounds=["ss"]))
        await node.connect("0.2.0", "1.0.0")
        await eventually(lambda: len(node.delivered) == 1)
        self.assertEqual(node._sync_retries, {})

        # Held, then changed through this controller: the held payload goes at once.
        await node.update_user(User(email="b", inbounds=["ss"]))
        await eventually(lambda: "b" in node._sync_retries)
        await node.update_user(User(email="b", inbounds=["vless"]))
        self.assertNotIn("b", node._sync_retries)
        await eventually(lambda: len(node.delivered) == 2)

        # Held, then changed by another producer writing to the shared store: gone once claimed.
        await node.update_user(User(email="c", inbounds=["ss"]))
        await eventually(lambda: "c" in node._sync_retries)
        await store.enqueue_users("node", [User(email="c", inbounds=["vless"])])
        await node.wake_sync_worker()
        await eventually(lambda: len(node.delivered) == 3)
        self.assertEqual(node._sync_retries, {})

        sent = len(node.delivered)
        await asyncio.sleep(0.1)  # several retry periods: no old payload is sent after the new one
        self.assertEqual(len(node.delivered), sent)
        self.assertEqual([list(user.inbounds) for user in node.delivered], [["vless"]] * 3)

    async def test_change_during_a_retry_is_not_overwritten_by_its_outcome(self):
        node = self.node()
        self.fast_retries(node)
        self.refuse(node, lambda user: "ss" in user.inbounds)
        refusing = node.sync_users_chunked

        async def send(users, **kwargs):
            if [user.email for user in users] == ["a"] and "a" in node._sync_retries:
                # This is the retry of the held payload; a newer change arrives meanwhile.
                await node.update_user(User(email="a", inbounds=["vless"]))
            return await refusing(users, **kwargs)

        node.sync_users_chunked = send
        await node.update_user(User(email="a", inbounds=["ss"]))
        await node.connect("0.2.0", "1.0.0")
        await eventually(lambda: len(node.delivered) == 1)
        self.assertEqual(list(node.delivered[0].inbounds), ["vless"])
        await asyncio.sleep(0.1)
        self.assertEqual(node._sync_retries, {})
        self.assertEqual(len(node.delivered), 1)

    async def test_rejected_users_wait_for_a_full_sync_fence(self):
        store = InMemoryUserSyncStore()
        node = self.node(store)
        self.fast_retries(node)
        sends = self.refuse(node, lambda user: user.email == "a")
        await node.update_user(User(email="a", inbounds=["ss"]))
        await node.connect("0.2.0", "1.0.0")
        await eventually(lambda: "a" in node._sync_retries)
        fence = await store.begin_full_sync("node", "snapshot", 30)
        await asyncio.sleep(0.02)  # let a retry already past its fence check finish
        count = len(sends)
        await asyncio.sleep(0.1)
        self.assertEqual(len(sends), count)
        await store.end_full_sync("node", fence)
        await eventually(lambda: len(sends) > count)

    async def test_transport_failure_while_isolating_retries_the_rest_whole(self):
        node = self.node()
        sends = []

        async def send(users, **kwargs):
            sends.append([user.email for user in users])
            if len(sends) == 1:
                return FailedUsers(users, rejected=True, detail="refused")
            if len(sends) == 2:
                return FailedUsers(users, detail="connection reset")
            node.delivered.extend(users)
            return []

        node.sync_users_chunked = send
        with self.fast_backoff():
            await node.update_users([User(email=email, inbounds=["vless"]) for email in "abcd"])
            await node.connect("0.2.0", "1.0.0")
            await eventually(lambda: len(node.delivered) == 4)
        self.assertEqual(sends, [list("abcd"), list("ab"), list("abcd")])
        self.assertEqual(node._sync_retries, {})

    async def test_disconnect_during_delivery_requeues_owned_claim(self):
        store = InMemoryUserSyncStore()
        node = self.node(store)
        entered = asyncio.Event()

        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        node.sync_users_chunked = blocked
        await node.update_user(User(email="a"))
        await node.connect("0.2.0", "1.0.0")
        await entered.wait()
        await node.disconnect()
        sibling = self.node(store)
        await sibling.connect("0.2.0", "1.0.0")
        await eventually(lambda: len(sibling.delivered) == 1)

    async def test_resolver_supplies_current_state_and_missing_identity_retries(self):
        resolver = AsyncMock(side_effect=[[], [User(email="a", inbounds=["current"])]])
        node = self.node(user_sync_resolver=resolver)
        await node.update_user(User(email="a", inbounds=["stale"]))
        await node.connect("0.2.0", "1.0.0")
        await eventually(lambda: len(node.delivered) == 1)
        self.assertEqual(list(node.delivered[0].inbounds), ["current"])
        self.assertEqual(resolver.await_count, 2)

    async def test_lease_deadline_includes_resolver_and_transport(self):
        resolver_cancelled = asyncio.Event()

        async def slow_resolver(*args):
            try:
                await asyncio.Event().wait()
            finally:
                resolver_cancelled.set()

        node = self.node(sync_lease_seconds=0.2, user_sync_resolver=slow_resolver)
        await node.update_user(User(email="a"))
        start = asyncio.get_running_loop().time()
        await node.connect("0.2.0", "1.0.0")
        await asyncio.wait_for(resolver_cancelled.wait(), 0.5)
        self.assertLess(asyncio.get_running_loop().time() - start, 0.4)
        self.assertEqual(node.delivered, [])
        self.assertTrue(await node._user_sync_store.has_pending("node"))

    async def test_snapshot_waits_for_sibling_delivery_and_preserves_newer_delta(self):
        store = InMemoryUserSyncStore()
        worker, snapshot = self.node(store), self.node(store)
        entered, release = asyncio.Event(), asyncio.Event()
        original_send = worker.sync_users_chunked

        async def blocked(users, **kwargs):
            entered.set()
            await release.wait()
            return await original_send(users, **kwargs)

        worker.sync_users_chunked = blocked
        await worker.update_user(User(email="a", inbounds=["old"]))
        await worker.connect("0.2.0", "1.0.0")
        await entered.wait()

        async def load():
            self.assertTrue(release.is_set())
            await worker.update_user(User(email="a", inbounds=["new"]))
            return [User(email="a", inbounds=["snapshot"])]

        loader = AsyncMock(side_effect=load)
        task = asyncio.create_task(snapshot.sync_users_from_source(loader))
        await asyncio.sleep(0.03)
        loader.assert_not_awaited()
        release.set()
        await asyncio.wait_for(task, 2)
        await eventually(lambda: len(worker.delivered) == 2)
        self.assertEqual(list(worker.delivered[-1].inbounds), ["new"])
        self.assertEqual(list(snapshot.snapshots[0][0].inbounds), ["snapshot"])
        self.assertFalse(await store.has_pending("node"))

    async def test_failed_snapshot_retires_nothing(self):
        store = InMemoryUserSyncStore()
        node = self.node(store)
        await store.enqueue_users("node", [User(email="a")])
        node.sync_users = AsyncMock(side_effect=OSError("snapshot failed"))
        with self.assertRaises(OSError):
            await node.sync_users_from_source(AsyncMock(return_value=[User(email="a")]))
        self.assertTrue(await store.has_pending("node"))
        self.assertFalse(await node.full_sync_in_progress())

    async def test_successful_snapshot_retires_covered_work_without_delta_replay(self):
        store = InMemoryUserSyncStore()
        node = self.node(store)
        user = User(email="a")
        await store.enqueue_users("node", [user])
        await node.sync_users_from_source(AsyncMock(return_value=[user]))
        self.assertEqual(node.snapshots, [[user]])
        self.assertFalse(await store.has_pending("node"))
        await node.connect("0.2.0", "1.0.0")
        await asyncio.sleep(0.03)
        self.assertEqual(node.delivered, [])

    async def test_uncertain_claim_timeout_recovers_after_lease_without_another_enqueue(self):
        store = InMemoryUserSyncStore()
        node = self.node(store, sync_lease_seconds=0.2)
        original = store.claim_users
        attempts = 0
        cancelled = asyncio.Event()

        async def uncertain_claim(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            result = await original(*args, **kwargs)
            if attempts == 1:
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            return result

        store.claim_users = uncertain_claim
        await node.update_user(User(email="a"))
        await node.connect("0.2.0", "1.0.0")
        await asyncio.wait_for(cancelled.wait(), 0.8)
        self.assertEqual(node.delivered, [])
        await eventually(lambda: len(node.delivered) == 1)
        self.assertFalse(await store.has_pending("node"))

    async def test_empty_queue_worker_exits_instead_of_polling_forever(self):
        store = InMemoryUserSyncStore()
        node = self.node(store)
        node._worker_idle_timeout = 0.02
        store.claim_users = AsyncMock(wraps=store.claim_users)
        await node.connect("0.2.0", "1.0.0")
        await eventually(lambda: node._sync_worker_task is None)
        self.assertEqual(store.claim_users.await_count, 1)
        await node.update_user(User(email="a"))
        await eventually(lambda: len(node.delivered) == 1)

    async def test_fence_renewal_loss_cancels_snapshot_owner(self):
        store = InMemoryUserSyncStore()
        node = self.node(store)
        store.renew_full_sync = AsyncMock(return_value=False)
        with self.assertRaises(NodeAPIError) as error:
            async with node.full_sync_fence(lease_seconds=0.09):
                await asyncio.Event().wait()
        self.assertEqual(error.exception.code, 409)
        self.assertFalse(await node.full_sync_in_progress())

    async def test_hung_fence_renewal_is_bounded_by_ownership_deadline(self):
        store = InMemoryUserSyncStore()
        node = self.node(store)

        async def hung_renewal(*args):
            await asyncio.Event().wait()

        store.renew_full_sync = hung_renewal
        async with asyncio.timeout(0.8):
            with self.assertRaises(NodeAPIError):
                async with node.full_sync_fence(lease_seconds=0.09):
                    await asyncio.Event().wait()

    async def test_fence_blocks_claims_and_concurrent_snapshot(self):
        store = InMemoryUserSyncStore()
        node, sibling = self.node(store), self.node(store)
        await store.enqueue_users("node", [User(email="a")])
        async with node.full_sync_fence():
            self.assertEqual(await store.claim_users("node", "other", 10, 30), [])
            with self.assertRaises(NodeAPIError):
                async with sibling.full_sync_fence():
                    self.fail("concurrent snapshot acquired the same node")
        self.assertEqual(len(await store.claim_users("node", "other", 10, 30)), 1)

    async def test_snapshot_starting_during_claim_defers_without_counting_failure(self):
        store = InMemoryUserSyncStore()
        node = self.node(store)
        claim = store.claim_users
        fence = None

        async def racing_claim(*args, **kwargs):
            nonlocal fence
            result = await claim(*args, **kwargs)
            if result and fence is None:
                fence = await store.begin_full_sync("node", "snapshot", 30)
            return result

        store.claim_users = racing_claim
        await node.update_user(User(email="a"))
        await node.connect("0.2.0", "1.0.0")
        await eventually(lambda: fence is not None)
        await asyncio.sleep(0.03)
        self.assertEqual(node.delivered, [])
        self.assertEqual(node._user_sync_failure_count, 0)
        await store.end_full_sync("node", fence)
        await eventually(lambda: len(node.delivered) == 1)

    async def test_unconfirmed_ack_requests_durable_refresh(self):
        store = InMemoryUserSyncStore()
        node = self.node(store)
        store.ack_users = AsyncMock(return_value=["a"])
        store.request_refresh = AsyncMock()
        await store.enqueue_users("node", [User(email="a")])
        claimed = await node._claim_pending_users()
        await node._ack_claimed_users(claimed)
        store.request_refresh.assert_awaited_once_with("node", ["a"])

    async def test_legacy_store_keeps_queue_api_but_rejects_uncoordinated_snapshot(self):
        store = InMemoryUserSyncStore()

        class LegacyStore:
            enqueue_users = store.enqueue_users
            claim_users = store.claim_users
            ack_users = store.ack_users
            requeue_users = store.requeue_users
            clear = store.clear

        node = self.node(LegacyStore())
        await node.update_user(User(email="a"))
        await node.connect("0.2.0", "1.0.0")
        await eventually(lambda: len(node.delivered) == 1)
        with self.assertRaises(NodeAPIError):
            await node.sync_users_from_source(AsyncMock(return_value=[]))

    async def test_invalid_queue_limits_are_rejected(self):
        for kwargs in ({"sync_batch_size": 0}, {"sync_chunk_size": -1}, {"sync_lease_seconds": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.node(**kwargs)
