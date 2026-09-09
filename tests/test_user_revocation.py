import asyncio
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PasarGuardNodeBridge.common import service_pb2 as service
from PasarGuardNodeBridge.common.service_pb2 import User
from PasarGuardNodeBridge.controller import Controller, Health, NodeAPIError
from PasarGuardNodeBridge.grpclib import Node as GrpcNode
from PasarGuardNodeBridge.rest import Node as RestNode
from PasarGuardNodeBridge.storage import (
    InMemoryUserSyncStore,
    UserRevocationConflictError,
    UserSyncLeaseLostError,
)


def _user(key: str, inbound: str = "active") -> User:
    return User(email=key, inbounds=[inbound] if inbound else [])


def _controller(store, worker_id: str = "worker-1") -> Controller:
    controller = object.__new__(Controller)
    controller.name = "test"
    controller.node_id = "node-1"
    controller.worker_id = worker_id
    controller.logger = logging.getLogger("test-user-revocation")
    controller._user_sync_store = store
    controller._sync_lease_seconds = 1.0
    controller._internal_timeout = 1
    controller._work_available = asyncio.Event()
    controller._shutdown_event = asyncio.Event()
    controller._worker_idle_timeout = 30.0
    controller._sync_poll_interval = 0.0
    controller._health = Health.HEALTHY
    controller._user_sync_epoch_supported = True
    controller._health_lock = asyncio.Lock()
    controller._node_lock = asyncio.Lock()
    controller._sync_worker_lock = asyncio.Lock()
    controller._sync_worker_task = None
    controller._ensure_sync_worker_running = AsyncMock()
    controller._hard_reset_event = asyncio.Event()
    controller._user_sync_failure_count = 0
    controller._hard_reset_threshold = 5
    controller._failure_count_lock = asyncio.Lock()
    controller._supports_chunked_sync = AsyncMock(return_value=(False, "0.1.0"))
    return controller


async def _wait_until(predicate, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("condition was not met")
        await asyncio.sleep(0.001)


class UserRevocationStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_abort_and_finalize_do_not_wait_for_active_startup(self):
        store = InMemoryUserSyncStore()
        startup = await store.acquire_startup_user_sync_lease("node-1", "starter", ["42"], 30)

        await asyncio.wait_for(store.abort_user_revocation("node-1", ["42"], "unknown"), timeout=0.1)
        await asyncio.wait_for(store.finalize_user_revocation("node-1", ["42"], "unknown"), timeout=0.1)

        await store.release_user_sync_lease(startup.lease)

    async def test_begin_discards_pending_and_blocks_new_updates(self):
        store = InMemoryUserSyncStore()
        controller = _controller(store)
        await store.enqueue_users("node-1", [_user("42")])

        await controller.begin_user_revocation(["42"], "revoke-a")
        await controller.update_user(_user("42", "new"))

        self.assertEqual(await store.claim_users("node-1", "reader", 10, 30), [])

    async def test_stale_claim_is_rejected_after_abort_and_authoritative_restore_wins(self):
        store = InMemoryUserSyncStore()
        controller = _controller(store)
        await store.enqueue_users("node-1", [_user("42", "stale")])
        stale_claim = (await store.claim_users("node-1", "old-worker", 10, 30))[0]

        await controller.begin_user_revocation(["42"], "revoke-a")
        await controller.abort_user_revocation(["42"], "revoke-a")

        stale_lease = await store.acquire_user_sync_lease(
            "node-1",
            "old-worker",
            ["42"],
            30,
            {"42": stale_claim.generation},
        )
        self.assertEqual(stale_lease.user_keys, ())

        await store.enqueue_users("node-1", [_user("42", "restored")])
        restored = await store.claim_users("node-1", "new-worker", 10, 30)
        self.assertEqual([list(item.user.inbounds) for item in restored], [["restored"]])

    async def test_overlapping_revocations_fail_without_releasing_owner(self):
        store = InMemoryUserSyncStore()
        controller = _controller(store)

        await controller.begin_user_revocation(["42"], "revoke-a")
        with self.assertRaises(UserRevocationConflictError) as error:
            await controller.begin_user_revocation(["43", "42"], "revoke-b")
        self.assertEqual(error.exception.conflicting_user_keys, ("42",))

        await controller.update_users([_user("42"), _user("43")])
        unfenced = await store.claim_users("node-1", "reader", 10, 30)
        self.assertEqual([item.user.email for item in unfenced], ["43"])

        await controller.abort_user_revocation(["42"], "revoke-a")
        result = await controller.begin_user_revocation(["42"], "revoke-b")
        self.assertEqual(result.active_user_keys, ("42",))
        await controller.abort_user_revocation(["42"], "revoke-b")
        await controller.update_user(_user("42", "restored"))
        claimed = await store.claim_users("node-1", "reader", 10, 30)
        self.assertEqual([list(item.user.inbounds) for item in claimed], [["restored"]])

    async def test_finalize_is_permanent_and_later_abort_cannot_reopen(self):
        store = InMemoryUserSyncStore()
        controller = _controller(store)

        await controller.begin_user_revocation(["42"], "revoke-a")
        await controller.finalize_user_revocation(["42"], "revoke-a")
        result = await controller.begin_user_revocation(["42", "43"], "revoke-b")
        self.assertEqual(result.active_user_keys, ("43",))
        self.assertEqual(result.finalized_user_keys, ("42",))
        await controller.update_user(_user("42"))

        self.assertEqual(await store.claim_users("node-1", "reader", 10, 30), [])

    async def test_abort_closes_admission_and_waits_authorized_write(self):
        store = InMemoryUserSyncStore()
        controller = _controller(store)
        await controller.begin_user_revocation(["42"], "revoke-a")
        lease = await store.acquire_user_sync_lease("node-1", "writer", ["42"], 30, revocation_id="revoke-a")

        abort = asyncio.create_task(controller.abort_user_revocation(["42"], "revoke-a"))
        await asyncio.sleep(0)
        self.assertFalse(abort.done())
        denied = await store.acquire_user_sync_lease("node-1", "late-writer", ["42"], 30, revocation_id="revoke-a")
        self.assertEqual(denied.user_keys, ())

        await store.release_user_sync_lease(lease)
        await asyncio.wait_for(abort, timeout=1)
        allowed = await store.acquire_user_sync_lease("node-1", "ordinary", ["42"], 30)
        self.assertEqual(allowed.user_keys, ("42",))
        await store.release_user_sync_lease(allowed)

    async def test_finalize_closes_admission_and_waits_authorized_write(self):
        store = InMemoryUserSyncStore()
        controller = _controller(store)
        await controller.begin_user_revocation(["42"], "revoke-a")
        lease = await store.acquire_user_sync_lease("node-1", "writer", ["42"], 30, revocation_id="revoke-a")

        finalize = asyncio.create_task(controller.finalize_user_revocation(["42"], "revoke-a"))
        await asyncio.sleep(0)
        self.assertFalse(finalize.done())
        denied = await store.acquire_user_sync_lease("node-1", "late-writer", ["42"], 30, revocation_id="revoke-a")
        self.assertEqual(denied.user_keys, ())

        await store.release_user_sync_lease(lease)
        await asyncio.wait_for(finalize, timeout=1)
        result = await controller.begin_user_revocation(["42"], "revoke-b")
        self.assertEqual(result.active_user_keys, ())
        self.assertEqual(result.finalized_user_keys, ("42",))

    async def test_abort_lost_lease_reopens_only_owner_admission(self):
        store = InMemoryUserSyncStore()
        controller = _controller(store)
        await controller.begin_user_revocation(["42"], "revoke-a")
        lost = await store.acquire_user_sync_lease("node-1", "writer", ["42"], 0.01, revocation_id="revoke-a")
        await asyncio.sleep(0.02)

        with self.assertRaises(UserSyncLeaseLostError):
            await controller.abort_user_revocation(["42"], "revoke-a")

        owner = await store.acquire_user_sync_lease("node-1", "reconcile", ["42"], 30, revocation_id="revoke-a")
        ordinary = await store.acquire_user_sync_lease("node-1", "ordinary", ["42"], 30)
        self.assertEqual(owner.user_keys, ("42",))
        self.assertEqual(ordinary.user_keys, ())
        await store.release_user_sync_lease(owner)
        await store.release_user_sync_lease(lost)
        await controller.abort_user_revocation(["42"], "revoke-a")
        admitted = await store.acquire_user_sync_lease("node-1", "ordinary-after-abort", ["42"], 30)
        self.assertEqual(admitted.user_keys, ("42",))
        await store.release_user_sync_lease(admitted)

    async def test_finalize_lost_lease_reopens_only_owner_admission(self):
        store = InMemoryUserSyncStore()
        controller = _controller(store)
        await controller.begin_user_revocation(["42"], "revoke-a")
        lost = await store.acquire_user_sync_lease("node-1", "writer", ["42"], 0.01, revocation_id="revoke-a")
        await asyncio.sleep(0.02)

        with self.assertRaises(UserSyncLeaseLostError):
            await controller.finalize_user_revocation(["42"], "revoke-a")

        owner = await store.acquire_user_sync_lease("node-1", "reconcile", ["42"], 30, revocation_id="revoke-a")
        ordinary = await store.acquire_user_sync_lease("node-1", "ordinary", ["42"], 30)
        self.assertEqual(owner.user_keys, ("42",))
        self.assertEqual(ordinary.user_keys, ())
        await store.release_user_sync_lease(owner)
        await store.release_user_sync_lease(lost)
        await controller.finalize_user_revocation(["42"], "revoke-a")
        result = await controller.begin_user_revocation(["42"], "revoke-b")
        self.assertEqual(result.finalized_user_keys, ("42",))

    async def test_abort_cancellation_reopens_only_owner_admission(self):
        store = InMemoryUserSyncStore()
        controller = _controller(store)
        await controller.begin_user_revocation(["42"], "revoke-a")
        active = await store.acquire_user_sync_lease("node-1", "writer", ["42"], 30, revocation_id="revoke-a")
        abort = asyncio.create_task(controller.abort_user_revocation(["42"], "revoke-a"))
        await asyncio.sleep(0)
        abort.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await abort

        owner = await store.acquire_user_sync_lease("node-1", "reconcile", ["42"], 30, revocation_id="revoke-a")
        ordinary = await store.acquire_user_sync_lease("node-1", "ordinary", ["42"], 30)
        self.assertEqual(owner.user_keys, ("42",))
        self.assertEqual(ordinary.user_keys, ())
        await store.release_user_sync_lease(owner)
        await store.release_user_sync_lease(active)
        await controller.abort_user_revocation(["42"], "revoke-a")

    async def test_finalize_cancellation_reopens_only_owner_admission(self):
        store = InMemoryUserSyncStore()
        controller = _controller(store)
        await controller.begin_user_revocation(["42"], "revoke-a")
        active = await store.acquire_user_sync_lease("node-1", "writer", ["42"], 30, revocation_id="revoke-a")
        finalize = asyncio.create_task(controller.finalize_user_revocation(["42"], "revoke-a"))
        await asyncio.sleep(0)
        finalize.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await finalize

        owner = await store.acquire_user_sync_lease("node-1", "reconcile", ["42"], 30, revocation_id="revoke-a")
        ordinary = await store.acquire_user_sync_lease("node-1", "ordinary", ["42"], 30)
        self.assertEqual(owner.user_keys, ("42",))
        self.assertEqual(ordinary.user_keys, ())
        await store.release_user_sync_lease(owner)
        await store.release_user_sync_lease(active)
        await controller.finalize_user_revocation(["42"], "revoke-a")

    async def test_cancelled_begin_stays_fail_closed_until_explicit_abort(self):
        store = InMemoryUserSyncStore()
        lease = await store.acquire_user_sync_lease("node-1", "worker", ["42"], 30)
        begin = asyncio.create_task(store.begin_user_revocation("node-1", ["42"], "revoke-a"))
        await asyncio.sleep(0)
        self.assertFalse(begin.done())

        begin.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await begin
        denied = await store.acquire_user_sync_lease("node-1", "reader", ["42"], 30)
        self.assertEqual(denied.user_keys, ())

        await store.release_user_sync_lease(lease)
        await store.abort_user_revocation("node-1", ["42"], "revoke-a")
        allowed = await store.acquire_user_sync_lease("node-1", "reader", ["42"], 30)
        self.assertEqual(allowed.user_keys, ("42",))

    async def test_expired_execution_lease_fails_closed_until_explicit_release(self):
        store = InMemoryUserSyncStore()
        lease = await store.acquire_user_sync_lease("node-1", "worker", ["42"], 0.01)
        await asyncio.sleep(0.02)

        with self.assertRaises(UserSyncLeaseLostError):
            await store.begin_user_revocation("node-1", ["42"], "revoke-a")
        denied = await store.acquire_user_sync_lease("node-1", "reader", ["42"], 30)
        self.assertEqual(denied.user_keys, ())  # begin already installed the fail-closed fence

        await store.release_user_sync_lease(lease)
        await store.begin_user_revocation("node-1", ["42"], "revoke-a")

    async def test_expired_execution_lease_is_recovered_by_authoritative_snapshot(self):
        for node_type in (RestNode, GrpcNode):
            with self.subTest(node_type=node_type.__module__):
                store = InMemoryUserSyncStore()
                controller = _controller(store)
                controller._default_timeout = 1
                lost = await store.acquire_user_sync_lease("node-1", "dead-worker", ["42"], 0.01)
                await asyncio.sleep(0.02)
                captured: list[str] = []

                async def transport(_captured=captured, **kwargs):
                    request = kwargs["proto_message"] if "proto_message" in kwargs else kwargs["request"]
                    _captured.extend(user.email for user in request.users)
                    return service.Empty()

                if node_type is RestNode:
                    controller._make_request = transport
                else:
                    controller._client = SimpleNamespace(SyncUsers=object())
                    controller._handle_grpc_request = transport

                await node_type.reconcile_users(controller, [_user("42"), _user("43")])

                self.assertEqual(captured, ["42", "43"])
                self.assertNotIn(lost.token, store._user_sync_leases)
                result = await controller.begin_user_revocation(["42"], "revoke-a")
                self.assertEqual(result.active_user_keys, ("42",))

    async def test_failed_reconciliation_retains_new_node_wide_poison(self):
        store = InMemoryUserSyncStore()
        controller = _controller(store)
        controller._default_timeout = 1
        controller._sync_lease_seconds = 0.01
        await store.acquire_user_sync_lease("node-1", "dead-worker", ["42"], 0.01)
        await asyncio.sleep(0.02)
        controller._make_request = AsyncMock(side_effect=asyncio.TimeoutError)

        with self.assertRaises(asyncio.TimeoutError):
            await RestNode.reconcile_users(controller, [_user("42")])
        await asyncio.sleep(0.02)

        with self.assertRaises(UserSyncLeaseLostError):
            await controller.begin_user_revocation(["unrelated"], "revoke-b")

    async def test_execution_lease_on_other_node_does_not_block_begin(self):
        store = InMemoryUserSyncStore()
        other_node_lease = await store.acquire_user_sync_lease("node-2", "worker", ["42"], 30)

        await asyncio.wait_for(
            store.begin_user_revocation("node-1", ["42"], "revoke-a"),
            timeout=0.1,
        )
        await store.release_user_sync_lease(other_node_lease)

    async def test_direct_sync_requires_matching_revocation_owner(self):
        store = InMemoryUserSyncStore()
        controller = _controller(store)
        await controller.begin_user_revocation(["42"], "revoke-a")

        with self.assertRaises(NodeAPIError) as error:
            await controller._acquire_direct_user_sync_lease([_user("42")])
        self.assertEqual(error.exception.code, 409)

        lease, heartbeat = await controller._acquire_direct_user_sync_lease([_user("42")], revocation_id="revoke-a")
        self.assertEqual(lease.user_keys, ("42",))
        await controller._release_user_sync_lease(lease, heartbeat)

    async def test_ambiguous_full_snapshot_timeout_retains_node_wide_poison(self):
        store = InMemoryUserSyncStore()
        controller = _controller(store)
        controller._sync_lease_seconds = 0.01
        controller._default_timeout = 1
        controller._node_lock = asyncio.Lock()
        controller._make_request = AsyncMock(side_effect=asyncio.TimeoutError)

        with self.assertRaises(asyncio.TimeoutError):
            await RestNode.sync_users(controller, [_user("42")])
        await asyncio.sleep(0.02)

        with self.assertRaises(UserSyncLeaseLostError):
            await store.acquire_user_sync_lease("node-1", "retry", ["other-user"], 30)
        with self.assertRaises(UserSyncLeaseLostError):
            await controller.begin_user_revocation(["42"], "revoke-a")

    async def test_full_snapshot_rejects_revocation_id(self):
        controller = _controller(InMemoryUserSyncStore())
        for node_type in (RestNode, GrpcNode):
            with self.subTest(node_type=node_type.__module__):
                with self.assertRaises(NodeAPIError) as error:
                    await node_type.sync_users(controller, [_user("42")], revocation_id="revoke-a")
                self.assertEqual(error.exception.code, 400)


class UserRevocationWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_during_store_release_waits_for_cleanup_then_propagates(self):
        class SlowReleaseStore(InMemoryUserSyncStore):
            def __init__(self):
                super().__init__()
                self.release_entered = asyncio.Event()
                self.finish_release = asyncio.Event()

            async def release_user_sync_lease(self, lease):
                self.release_entered.set()
                await self.finish_release.wait()
                await super().release_user_sync_lease(lease)

        store = SlowReleaseStore()
        controller = _controller(store)
        lease = await store.acquire_user_sync_lease("node-1", "worker-1", ["42"], 30)
        release = asyncio.create_task(controller._release_user_sync_lease(lease))
        await asyncio.wait_for(store.release_entered.wait(), timeout=1)
        release.cancel()
        await asyncio.sleep(0)
        self.assertFalse(release.done())

        store.finish_release.set()
        with self.assertRaises(asyncio.CancelledError):
            await release
        self.assertNotIn(lease.token, store._user_sync_leases)

    async def test_cleanup_error_is_not_masked_by_caller_cancellation(self):
        class FailingReleaseStore(InMemoryUserSyncStore):
            def __init__(self):
                super().__init__()
                self.release_entered = asyncio.Event()
                self.finish_release = asyncio.Event()

            async def release_user_sync_lease(self, _lease):
                self.release_entered.set()
                await self.finish_release.wait()
                raise RuntimeError("release failed")

        store = FailingReleaseStore()
        controller = _controller(store)
        lease = await store.acquire_user_sync_lease("node-1", "worker-1", ["42"], 30)
        release = asyncio.create_task(controller._release_user_sync_lease(lease))
        await asyncio.wait_for(store.release_entered.wait(), timeout=1)
        release.cancel()
        store.finish_release.set()

        with self.assertRaisesRegex(RuntimeError, "release failed"):
            await release

    async def test_worker_retries_stale_epoch_with_new_lease_without_poison(self):
        store = InMemoryUserSyncStore()
        controller = _controller(store)
        observed_epochs = []

        async def sync_batch(_users, user_sync_epoch):
            observed_epochs.append(user_sync_epoch)
            if len(observed_epochs) == 1:
                raise NodeAPIError(412, "stale user sync epoch")
            controller._shutdown_event.set()
            return []

        controller._sync_batch_users = sync_batch
        await store.enqueue_users("node-1", [_user("42")])
        controller._work_available.set()

        async def no_delay(_seconds):
            return None

        with patch("PasarGuardNodeBridge.controller.asyncio.sleep", side_effect=no_delay):
            await controller._sync_worker()

        self.assertEqual(observed_epochs, [1, 2])
        self.assertEqual(store._user_sync_leases, {})
        self.assertIsNone(await store.next_claim_delay("node-1"))

    async def test_release_lease_propagates_caller_cancellation_after_cleanup(self):
        store = InMemoryUserSyncStore()
        controller = _controller(store)
        lease = await store.acquire_user_sync_lease("node-1", "worker-1", ["42"], 30)
        heartbeat_cancelling = asyncio.Event()

        async def slow_heartbeat_shutdown():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                heartbeat_cancelling.set()
                await asyncio.Event().wait()

        heartbeat = asyncio.create_task(slow_heartbeat_shutdown())
        release = asyncio.create_task(controller._release_user_sync_lease(lease, heartbeat))
        await asyncio.wait_for(heartbeat_cancelling.wait(), timeout=1)
        release.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await release
        self.assertNotIn(lease.token, store._user_sync_leases)

    async def test_abandon_lease_propagates_caller_cancellation_and_keeps_poison(self):
        store = InMemoryUserSyncStore()
        controller = _controller(store)
        lease = await store.acquire_user_sync_lease("node-1", "worker-1", ["42"], 30)
        heartbeat_cancelling = asyncio.Event()

        async def slow_heartbeat_shutdown():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                heartbeat_cancelling.set()
                await asyncio.Event().wait()

        heartbeat = asyncio.create_task(slow_heartbeat_shutdown())
        abandon = asyncio.create_task(controller._abandon_user_sync_lease(lease, heartbeat))
        await asyncio.wait_for(heartbeat_cancelling.wait(), timeout=1)
        abandon.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await abandon
        self.assertIn(lease.token, store._user_sync_leases)

    async def test_narrowing_stops_original_heartbeat_before_replacing_lease(self):
        class YieldAfterNarrowStore(InMemoryUserSyncStore):
            async def retain_user_sync_lease_keys(self, lease, retained_user_keys):
                narrowed = await super().retain_user_sync_lease_keys(lease, retained_user_keys)
                await asyncio.sleep(0.02)
                return narrowed

        store = YieldAfterNarrowStore()
        controller = _controller(store)
        controller._sync_lease_seconds = 0.03
        lease, heartbeat = await controller._acquire_direct_user_sync_lease([_user("ok"), _user("failed")])

        await controller._retain_unknown_user_sync_lease_keys(lease, heartbeat, ["failed"])

        self.assertTrue(heartbeat.done())
        if not heartbeat.cancelled():
            self.assertIsNone(heartbeat.exception())
        retained, _ = store._user_sync_leases[lease.token]
        self.assertEqual(retained.user_keys, ("failed",))

    async def test_partial_failure_retains_poison_only_for_unknown_key(self):
        store = InMemoryUserSyncStore()
        controller = _controller(store)
        controller._sync_lease_seconds = 0.01
        lease = await controller._acquire_user_sync_lease(["ok", "failed"])

        await controller._retain_unknown_user_sync_lease_keys(lease, None, ["failed"])

        ok_result = await controller.begin_user_revocation(["ok"], "revoke-ok")
        self.assertEqual(ok_result.active_user_keys, ("ok",))
        await controller.abort_user_revocation(["ok"], "revoke-ok")
        await asyncio.sleep(0.11)
        with self.assertRaises(UserSyncLeaseLostError):
            await controller.begin_user_revocation(["failed"], "revoke-failed")

    async def test_partial_failure_split_is_atomic_with_concurrent_begin(self):
        store = InMemoryUserSyncStore()
        controller = _controller(store)
        controller._sync_lease_seconds = 0.01
        lease = await controller._acquire_user_sync_lease(["ok", "failed"])

        failed_begin = asyncio.create_task(controller.begin_user_revocation(["failed"], "revoke-failed"))
        await _wait_until(
            lambda: (
                (state := store._revocations.get("node-1", {}).get("failed")) is not None
                and state.active_owner == "revoke-failed"
            )
        )

        await controller._retain_unknown_user_sync_lease_keys(lease, None, ["failed"])

        ok_result = await controller.begin_user_revocation(["ok"], "revoke-ok")
        self.assertEqual(ok_result.active_user_keys, ("ok",))
        await controller.abort_user_revocation(["ok"], "revoke-ok")
        with self.assertRaises(UserSyncLeaseLostError):
            await asyncio.wait_for(failed_begin, timeout=1)

    async def test_begin_waits_for_claimed_inflight_sync(self):
        store = InMemoryUserSyncStore()
        controller = _controller(store)
        revoker = _controller(store, "worker-2")
        entered = asyncio.Event()
        release = asyncio.Event()
        applied: list[list[str]] = []

        async def sync_batch(users):
            entered.set()
            await release.wait()
            applied.extend(list(user.inbounds) for user in users)
            return []

        controller._sync_batch_users = sync_batch
        await store.enqueue_users("node-1", [_user("42", "stale")])
        controller._work_available.set()
        worker = asyncio.create_task(controller._sync_worker())
        controller._sync_worker_task = worker
        await asyncio.wait_for(entered.wait(), timeout=1)

        begin = asyncio.create_task(revoker.begin_user_revocation(["42"], "revoke-a"))
        await asyncio.sleep(0)
        self.assertFalse(begin.done())
        release.set()
        await asyncio.wait_for(begin, timeout=1)
        self.assertEqual(applied, [["stale"]])

        await revoker.finalize_user_revocation(["42"], "revoke-a")
        await controller.update_user(_user("42", "late"))
        worker.cancel()
        await worker
        self.assertEqual(await store.claim_users("node-1", "reader", 10, 30), [])

    async def test_chunked_worker_uses_outer_lease_without_nested_admission_race(self):
        store = InMemoryUserSyncStore(max_pending_users_per_node=2000)
        controller = _controller(store)
        revoker = _controller(store, "worker-2")
        controller._supports_chunked_sync = AsyncMock(return_value=(True, "0.2.0"))
        entered = asyncio.Event()
        release = asyncio.Event()

        async def sync_chunked(_users, _chunk_size, _timeout):
            entered.set()
            await release.wait()

        controller._sync_users_chunked_transport = sync_chunked
        await store.enqueue_users("node-1", [_user(str(index)) for index in range(1000)])
        controller._work_available.set()
        worker = asyncio.create_task(controller._sync_worker())
        controller._sync_worker_task = worker
        await asyncio.wait_for(entered.wait(), timeout=1)

        begin = asyncio.create_task(revoker.begin_user_revocation(["0"], "revoke-a"))
        await asyncio.sleep(0)
        self.assertFalse(begin.done())
        release.set()
        result = await asyncio.wait_for(begin, timeout=1)
        self.assertEqual(result.active_user_keys, ("0",))
        self.assertFalse(worker.done())

        worker.cancel()
        await worker
        self.assertEqual(await store.claim_users("node-1", "reader", 2000, 30), [])

    async def test_legacy_subclass_without_transport_hook_falls_back_to_batch(self):
        store = InMemoryUserSyncStore(max_pending_users_per_node=2000)
        controller = _controller(store)
        revoker = _controller(store, "worker-2")
        controller._supports_chunked_sync = AsyncMock(return_value=(True, "0.2.0"))
        controller.sync_users_chunked = AsyncMock()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def sync_batch(_users):
            entered.set()
            await release.wait()
            return []

        controller._sync_batch_users = sync_batch
        await store.enqueue_users("node-1", [_user(str(index)) for index in range(1000)])
        controller._work_available.set()
        worker = asyncio.create_task(controller._sync_worker())
        controller._sync_worker_task = worker
        await asyncio.wait_for(entered.wait(), timeout=1)

        begin = asyncio.create_task(revoker.begin_user_revocation(["0"], "revoke-a"))
        await asyncio.sleep(0)
        self.assertFalse(begin.done())
        release.set()
        await asyncio.wait_for(begin, timeout=1)
        controller.sync_users_chunked.assert_not_awaited()

        worker.cancel()
        await worker

    async def test_worker_cancellation_cannot_requeue_stale_claim_after_begin(self):
        store = InMemoryUserSyncStore()
        controller = _controller(store)
        controller._sync_lease_seconds = 0.01
        entered = asyncio.Event()

        async def sync_batch(_users):
            entered.set()
            await asyncio.Event().wait()

        controller._sync_batch_users = sync_batch
        await store.enqueue_users("node-1", [_user("42", "stale")])
        controller._work_available.set()
        worker = asyncio.create_task(controller._sync_worker())
        controller._sync_worker_task = worker
        await asyncio.wait_for(entered.wait(), timeout=1)

        begin = asyncio.create_task(controller.begin_user_revocation(["42"], "revoke-a"))
        await asyncio.sleep(0)
        worker.cancel()
        await worker
        with self.assertRaises(UserSyncLeaseLostError):
            await asyncio.wait_for(begin, timeout=1)

        self.assertEqual(await store.claim_users("node-1", "reader", 10, 30), [])


class StartupRevocationTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _startup_controller(store):
        controller = _controller(store)
        controller._default_timeout = 1
        controller.get_health = AsyncMock(return_value=Health.HEALTHY)
        controller._acquire_lifecycle_lease = AsyncMock(return_value=None)
        controller._release_lifecycle_lease = AsyncMock()
        controller.connect = AsyncMock()
        controller.disconnect = AsyncMock()
        return controller

    async def _run_start(self, node_type, controller, transport, users=None, **kwargs):
        if node_type is RestNode:
            controller._make_request = transport
        else:
            controller._client = SimpleNamespace(Start=object())
            controller._handle_grpc_request = transport
        return await node_type.start(
            controller,
            config="{}",
            backend_type=service.BackendType.XRAY,
            users=users if users is not None else [_user("stale"), _user("keep")],
            **kwargs,
        )

    async def _run_full_sync(self, node_type, controller, transport, users=None):
        if node_type is RestNode:
            controller._make_request = transport
        else:
            controller._client = SimpleNamespace(SyncUsers=object())
            controller._handle_grpc_request = transport
        return await node_type.sync_users(
            controller,
            users=users if users is not None else [_user("stale"), _user("keep")],
        )

    async def test_begin_waits_for_startup_snapshot_already_inflight(self):
        for node_type in (RestNode, GrpcNode):
            with self.subTest(node_type=node_type.__module__):
                store = InMemoryUserSyncStore()
                controller = self._startup_controller(store)
                revoker = _controller(store, "revoker")
                entered = asyncio.Event()
                release = asyncio.Event()
                captured = []

                async def transport(
                    _captured=captured,
                    _entered=entered,
                    _release=release,
                    **kwargs,
                ):
                    request = kwargs["proto_message"] if "proto_message" in kwargs else kwargs["request"]
                    _captured.extend(user.email for user in request.users)
                    _entered.set()
                    await _release.wait()
                    return service.BaseInfoResponse(started=True, node_version="0.2.0", core_version="1.0.0")

                start = asyncio.create_task(self._run_start(node_type, controller, transport))
                await asyncio.wait_for(entered.wait(), timeout=1)
                begin = asyncio.create_task(revoker.begin_user_revocation(["stale"], "revoke-a"))
                await asyncio.sleep(0)
                self.assertFalse(begin.done())

                release.set()
                await asyncio.wait_for(start, timeout=1)
                result = await asyncio.wait_for(begin, timeout=1)
                self.assertEqual(result.active_user_keys, ("stale",))
                self.assertEqual(captured, ["stale", "keep"])
                await revoker.abort_user_revocation(["stale"], "revoke-a")

    async def test_authoritative_start_recovers_expired_node_wide_poison(self):
        for node_type in (RestNode, GrpcNode):
            with self.subTest(node_type=node_type.__module__):
                store = InMemoryUserSyncStore()
                controller = self._startup_controller(store)
                lost = (await store.acquire_startup_user_sync_lease("node-1", "dead-worker", ["stale"], 0.01)).lease
                await asyncio.sleep(0.02)
                captured: list[str] = []

                async def transport(_captured=captured, **kwargs):
                    request = kwargs["proto_message"] if "proto_message" in kwargs else kwargs["request"]
                    _captured.extend(user.email for user in request.users)
                    return service.BaseInfoResponse(started=True, node_version="0.2.0", core_version="1.0.0")

                await self._run_start(node_type, controller, transport, reconcile_user_sync=True)

                self.assertEqual(captured, ["stale", "keep"])
                self.assertNotIn(lost.token, store._user_sync_leases)

    async def test_failed_reconcile_leaves_recoverable_wildcard_poison(self):
        store = InMemoryUserSyncStore()
        controller = self._startup_controller(store)
        controller._sync_lease_seconds = 0.01

        async def failed_transport(**_kwargs):
            raise TimeoutError

        with self.assertRaises(TimeoutError):
            await self._run_start(
                RestNode,
                controller,
                failed_transport,
                reconcile_user_sync=True,
            )
        await asyncio.sleep(0.02)

        captured: list[int] = []

        async def successful_transport(**kwargs):
            request = kwargs["proto_message"]
            captured.append(request.user_sync_epoch)
            return service.BaseInfoResponse(
                started=True,
                node_version="0.2.0",
                core_version="1.0.0",
                user_sync_epoch_supported=True,
            )

        await self._run_start(
            RestNode,
            controller,
            successful_transport,
            reconcile_user_sync=True,
        )
        self.assertEqual(len(captured), 1)
        self.assertGreater(captured[0], 0)
        self.assertEqual(store._user_sync_leases, {})

    async def test_startup_waits_for_provisional_restore_and_abort(self):
        for node_type in (RestNode, GrpcNode):
            with self.subTest(node_type=node_type.__module__):
                store = InMemoryUserSyncStore()
                controller = self._startup_controller(store)
                await controller.begin_user_revocation(["stale"], "revoke-a")
                restore_lease = await store.acquire_user_sync_lease(
                    "node-1", "restore", ["stale"], 30, revocation_id="revoke-a"
                )
                entered = asyncio.Event()
                captured = []

                async def transport(_captured=captured, _entered=entered, **kwargs):
                    request = kwargs["proto_message"] if "proto_message" in kwargs else kwargs["request"]
                    _captured.extend(user.email for user in request.users)
                    _entered.set()
                    return service.BaseInfoResponse(started=True, node_version="0.2.0", core_version="1.0.0")

                start = asyncio.create_task(self._run_start(node_type, controller, transport))
                await asyncio.sleep(0)
                self.assertFalse(entered.is_set())

                abort = asyncio.create_task(controller.abort_user_revocation(["stale"], "revoke-a"))
                await asyncio.sleep(0)
                self.assertFalse(abort.done())
                await store.release_user_sync_lease(restore_lease)
                await asyncio.wait_for(abort, timeout=1)
                await asyncio.wait_for(start, timeout=1)
                self.assertEqual(captured, ["stale", "keep"])

    async def test_node_wide_start_blocks_begin_for_omitted_key(self):
        for node_type in (RestNode, GrpcNode):
            with self.subTest(node_type=node_type.__module__):
                store = InMemoryUserSyncStore()
                controller = self._startup_controller(store)
                revoker = _controller(store, "revoker")
                entered = asyncio.Event()
                release = asyncio.Event()

                async def transport(_entered=entered, _release=release, **_kwargs):
                    _entered.set()
                    await _release.wait()
                    return service.BaseInfoResponse(started=True, node_version="0.2.0", core_version="1.0.0")

                start = asyncio.create_task(self._run_start(node_type, controller, transport, users=[_user("keep")]))
                await asyncio.wait_for(entered.wait(), timeout=1)
                begin = asyncio.create_task(revoker.begin_user_revocation(["omitted"], "revoke-a"))
                await asyncio.sleep(0)
                self.assertFalse(begin.done())

                release.set()
                await asyncio.wait_for(start, timeout=1)
                await asyncio.wait_for(begin, timeout=1)
                await revoker.abort_user_revocation(["omitted"], "revoke-a")

    async def test_startup_and_incremental_leases_are_mutually_exclusive(self):
        for node_type in (RestNode, GrpcNode):
            with self.subTest(node_type=node_type.__module__):
                store = InMemoryUserSyncStore()
                controller = self._startup_controller(store)
                old_update = await store.acquire_user_sync_lease("node-1", "old-update", ["omitted"], 30)
                entered = asyncio.Event()
                release = asyncio.Event()

                async def transport(_entered=entered, _release=release, **_kwargs):
                    _entered.set()
                    await _release.wait()
                    return service.BaseInfoResponse(started=True, node_version="0.2.0", core_version="1.0.0")

                start = asyncio.create_task(self._run_start(node_type, controller, transport, users=[_user("keep")]))
                await asyncio.sleep(0)
                self.assertFalse(entered.is_set())

                new_update = asyncio.create_task(store.acquire_user_sync_lease("node-1", "new-update", ["omitted"], 30))
                await asyncio.sleep(0)
                self.assertFalse(new_update.done())

                await store.release_user_sync_lease(old_update)
                await asyncio.wait_for(entered.wait(), timeout=1)
                self.assertFalse(new_update.done())

                release.set()
                await asyncio.wait_for(start, timeout=1)
                admitted = await asyncio.wait_for(new_update, timeout=1)
                self.assertEqual(admitted.user_keys, ("omitted",))
                await store.release_user_sync_lease(admitted)

    async def test_startup_snapshot_filters_finalized_user(self):
        for node_type in (RestNode, GrpcNode):
            with self.subTest(node_type=node_type.__module__):
                store = InMemoryUserSyncStore()
                controller = self._startup_controller(store)
                await controller.begin_user_revocation(["stale"], "revoke-a")
                await controller.finalize_user_revocation(["stale"], "revoke-a")
                captured = []

                async def transport(_captured=captured, **kwargs):
                    request = kwargs["proto_message"] if "proto_message" in kwargs else kwargs["request"]
                    _captured.extend(user.email for user in request.users)
                    return service.BaseInfoResponse(started=True, node_version="0.2.0", core_version="1.0.0")

                await self._run_start(node_type, controller, transport)
                self.assertEqual(captured, ["keep"])

    async def test_full_sync_snapshot_is_node_wide_and_filters_finalized_user(self):
        for node_type in (RestNode, GrpcNode):
            with self.subTest(node_type=node_type.__module__):
                store = InMemoryUserSyncStore()
                controller = self._startup_controller(store)
                await controller.begin_user_revocation(["stale"], "revoke-a")
                await controller.finalize_user_revocation(["stale"], "revoke-a")
                captured = []

                async def transport(_captured=captured, **kwargs):
                    request = kwargs["proto_message"] if "proto_message" in kwargs else kwargs["request"]
                    _captured.extend(user.email for user in request.users)
                    return service.Empty()

                await self._run_full_sync(node_type, controller, transport)
                self.assertEqual(captured, ["keep"])

                empty_users, lease, heartbeat = await controller._acquire_snapshot_user_sync_lease([])
                self.assertEqual(empty_users, [])
                self.assertTrue(lease.covers_all_users)
                self.assertTrue(lease.token)
                await controller._release_user_sync_lease(lease, heartbeat)

    async def test_ambiguous_start_retains_user_sync_poison(self):
        for node_type in (RestNode, GrpcNode):
            with self.subTest(node_type=node_type.__module__):
                store = InMemoryUserSyncStore()
                controller = self._startup_controller(store)
                controller._sync_lease_seconds = 0.01

                async def transport(**_kwargs):
                    raise TimeoutError

                with self.assertRaises(TimeoutError):
                    await self._run_start(node_type, controller, transport)
                await asyncio.sleep(0.11)
                with self.assertRaises(UserSyncLeaseLostError):
                    await store.acquire_user_sync_lease("node-1", "late-update", ["omitted"], 30)
                with self.assertRaises(UserSyncLeaseLostError):
                    await controller.begin_user_revocation(["omitted"], "revoke-a")


class LegacyStoreCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_updates_work_but_revocation_fails_closed(self):
        class LegacyStore:
            def __init__(self):
                self.users = []

            async def enqueue_users(self, _node_id, users):
                self.users.extend(users)

        store = LegacyStore()
        controller = _controller(store)
        controller._ensure_sync_worker_running = AsyncMock()

        await controller.update_user(_user("42"))
        self.assertEqual([user.email for user in store.users], ["42"])
        with self.assertRaises(NodeAPIError) as error:
            await controller.begin_user_revocation(["42"], "revoke-a")
        self.assertEqual(error.exception.code, 501)


if __name__ == "__main__":
    unittest.main()
