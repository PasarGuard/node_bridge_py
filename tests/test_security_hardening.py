import asyncio
import io
import logging
import unittest
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from PasarGuardNodeBridge.aiohttp_compat import LazyClientSession
from PasarGuardNodeBridge.common.service_pb2 import User
from PasarGuardNodeBridge.controller import Controller, Health, NodeAPIError, _SanitizingLoggerAdapter
from PasarGuardNodeBridge.grpclib import Node as GrpcNode
from PasarGuardNodeBridge.rest import Node as RestNode
from PasarGuardNodeBridge.storage import ClaimedUser, InMemoryUserSyncStore


class _ResponseContext:
    def __init__(self, response=None):
        self.response = response if response is not None else object()

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _HangingStreamContext:
    async def __aenter__(self):
        await asyncio.Event().wait()

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _StreamMethod:
    def open(self, **kwargs):
        return _HangingStreamContext()


class _LifecycleStream:
    def __init__(self, hanging_phase):
        self.hanging_phase = hanging_phase

    async def send_message(self, message):
        if self.hanging_phase == "send":
            await asyncio.Event().wait()

    async def end(self):
        if self.hanging_phase == "end":
            await asyncio.Event().wait()


class _LifecycleStreamContext:
    def __init__(self, hanging_phase):
        self.hanging_phase = hanging_phase
        self.stream = _LifecycleStream(hanging_phase)

    async def __aenter__(self):
        return self.stream

    async def __aexit__(self, exc_type, exc, traceback):
        if self.hanging_phase == "exit":
            await asyncio.Event().wait()
        return False


class _LifecycleStreamMethod:
    def __init__(self, hanging_phase):
        self.hanging_phase = hanging_phase

    def open(self, **kwargs):
        return _LifecycleStreamContext(self.hanging_phase)


class _BrokenSendStream:
    def __init__(self):
        self.send_calls = 0

    async def send_message(self, message):
        self.send_calls += 1
        raise RuntimeError("stream closed")

    async def end(self):
        raise AssertionError("end must not be called after a send failure")


class _BrokenSendStreamContext:
    def __init__(self):
        self.stream = _BrokenSendStream()

    async def __aenter__(self):
        return self.stream

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _BrokenSendStreamMethod:
    def __init__(self):
        self.context = _BrokenSendStreamContext()

    def open(self, **kwargs):
        return self.context


class _HttpResponse:
    def __init__(self, status):
        self.status = status
        self.headers = {}
        self.url = "https://node.example/redirect"
        self.reason = "Redirect"
        self.charset = "utf-8"
        self.closed = False

    async def read(self):
        return b""

    def close(self):
        self.closed = True


class _StalledHttpResponse(_HttpResponse):
    async def read(self):
        await asyncio.Event().wait()


class _StaticRequestClient:
    def __init__(self, response):
        self.response = response

    def request(self, *args, **kwargs):
        return _ResponseContext(self.response)

    async def get(self, *args, **kwargs):
        return self.response


class RedirectPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_forces_redirects_off_even_if_caller_enables_them(self):
        session = MagicMock()
        session.request.return_value = _ResponseContext()
        client = LazyClientSession.__new__(LazyClientSession)
        client._get_session = AsyncMock(return_value=session)

        async with client.request("GET", "/info", allow_redirects=True):
            pass

        self.assertFalse(session.request.call_args.kwargs["allow_redirects"])

    async def test_get_forces_redirects_off(self):
        session = MagicMock()
        session.get = AsyncMock(return_value=object())
        client = LazyClientSession.__new__(LazyClientSession)
        client._get_session = AsyncMock(return_value=session)

        await client.get("/logs")

        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])

    async def test_rest_redirect_responses_are_explicit_node_api_errors(self):
        for status in (302, 307, 308):
            with self.subTest(status=status):
                node = RestNode.__new__(RestNode)
                node._client = _StaticRequestClient(_HttpResponse(status))

                with self.assertRaises(NodeAPIError) as error:
                    await node._make_request(method="GET", endpoint="info", timeout=1)

                self.assertEqual(error.exception.code, status)

    async def test_log_stream_redirect_is_an_explicit_node_api_error(self):
        node = RestNode.__new__(RestNode)
        node._client = _StaticRequestClient(_HttpResponse(302))
        node._internal_timeout = 0.1
        node.name = "node"
        node.logger = _SanitizingLoggerAdapter(logging.getLogger("test.log-redirect"), {})

        with self.assertRaises(NodeAPIError) as error:
            async with node.stream_logs():
                self.fail("redirected log stream must not open")

        self.assertEqual(error.exception.code, 302)

    async def test_log_stream_redirect_body_read_is_bounded_and_response_is_closed(self):
        response = _StalledHttpResponse(302)
        node = RestNode.__new__(RestNode)
        node._client = _StaticRequestClient(response)
        node._internal_timeout = 0.01
        node.name = "node"
        node.logger = _SanitizingLoggerAdapter(logging.getLogger("test.stalled-log-redirect"), {})

        with self.assertRaises(NodeAPIError):
            async with asyncio.timeout(0.2):
                async with node.stream_logs():
                    self.fail("redirected log stream must not open")

        self.assertTrue(response.closed)


class GrpcStreamTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_sync_stream_open_timeout_returns_users_for_retry_accounting(self):
        node = GrpcNode.__new__(GrpcNode)
        node._client = SimpleNamespace(SyncUser=_StreamMethod())
        node._metadata = {}
        node._internal_timeout = 0.01
        node.name = "node"
        node.logger = _SanitizingLoggerAdapter(logging.getLogger("test.grpc-timeout"), {})
        users = [User(email="private@example.com")]

        failed = await asyncio.wait_for(node._sync_batch_users(users), timeout=0.2)

        self.assertEqual(failed, users)

    async def test_user_sync_send_end_and_exit_are_bounded(self):
        users = [User(email="private@example.com")]
        for hanging_phase in ("send", "end", "exit"):
            with self.subTest(hanging_phase=hanging_phase):
                node = GrpcNode.__new__(GrpcNode)
                node._client = SimpleNamespace(SyncUser=_LifecycleStreamMethod(hanging_phase))
                node._metadata = {}
                node._internal_timeout = 0.01
                node.name = "node"
                node.logger = _SanitizingLoggerAdapter(logging.getLogger("test.grpc-lifecycle"), {})

                failed = await asyncio.wait_for(node._sync_batch_users(users), timeout=0.2)

                self.assertEqual(failed, users)

    async def test_user_sync_stops_after_first_broken_stream_send(self):
        method = _BrokenSendStreamMethod()
        node = GrpcNode.__new__(GrpcNode)
        node._client = SimpleNamespace(SyncUser=method)
        node._metadata = {}
        node._internal_timeout = 0.1
        node.name = "node"
        node.logger = _SanitizingLoggerAdapter(logging.getLogger("test.grpc-broken-stream"), {})
        users = [User(email=f"user-{index}@example.com") for index in range(3)]

        failed = await node._sync_batch_users(users)

        self.assertEqual(failed, users)
        self.assertEqual(method.context.stream.send_calls, 1)

    async def test_stream_open_timeout_increments_worker_failure_and_requeues(self):
        node = GrpcNode.__new__(GrpcNode)
        node._client = SimpleNamespace(SyncUser=_StreamMethod())
        node._metadata = {}
        node._internal_timeout = 0.01
        node.name = "node"
        node.logger = _SanitizingLoggerAdapter(logging.getLogger("test.grpc-accounting"), {})
        node._shutdown_event = asyncio.Event()
        node._work_available = asyncio.Event()
        node._work_available.set()
        node._worker_idle_timeout = 0.1
        node._sync_poll_interval = 0
        node._sync_lease_seconds = 30
        node._health = Health.HEALTHY
        node._health_lock = asyncio.Lock()
        node._failure_count_lock = asyncio.Lock()
        node._user_sync_failure_count = 0
        node._hard_reset_threshold = 5
        node._hard_reset_event = asyncio.Event()
        node._supports_chunked_sync = AsyncMock(return_value=(False, "0.1.0"))
        user = User(email="private@example.com")
        claimed = [ClaimedUser(token="claim", user=user)]
        node._claim_pending_users = AsyncMock(return_value=claimed)
        node._requeue_claimed_users = AsyncMock()
        node._ack_claimed_users = AsyncMock()
        node._sync_batch_users = MethodType(GrpcNode._sync_batch_users, node)

        async def stop_after_backoff(_delay):
            node._shutdown_event.set()

        with patch("PasarGuardNodeBridge.controller.asyncio.sleep", side_effect=stop_after_backoff):
            await node._sync_worker()

        self.assertEqual(node._user_sync_failure_count, 1)
        node._requeue_claimed_users.assert_awaited_once_with(claimed)


class LoggingSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_logger_does_not_install_output_handler(self):
        package_logger = logging.getLogger("PasarGuardNodeBridge")
        handlers_before = list(package_logger.handlers)
        ssl_context = MagicMock()

        with (
            patch("PasarGuardNodeBridge.controller.ssl.create_default_context", return_value=ssl_context),
            patch("PasarGuardNodeBridge.controller.LazyClientSession"),
        ):
            Controller(
                server_ca="certificate",
                api_key="00000000-0000-0000-0000-000000000000",
                service_url="https://node.example/",
            )

        self.assertEqual(package_logger.handlers, handlers_before)

    async def test_user_sync_log_omits_identifier_and_escapes_control_characters(self):
        records = []

        class _Handler(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        logger = logging.getLogger("test.log-safety")
        logger.handlers = [_Handler()]
        logger.propagate = False
        logger.setLevel(logging.WARNING)

        node = RestNode.__new__(RestNode)
        node.name = "node\nforged"
        node._internal_timeout = 1
        node._make_request = AsyncMock(side_effect=[object(), RuntimeError("remote\r\ninjected")])
        node.logger = _SanitizingLoggerAdapter(logger, {})

        failed = await node._sync_batch_users([User(email="first@example.com"), User(email="private@example.com")])

        self.assertEqual(len(failed), 1)
        self.assertEqual(len(records), 1)
        self.assertNotIn("private@example.com", records[0])
        self.assertNotIn("\n", records[0])
        self.assertNotIn("\r", records[0])
        self.assertIn("\\x0a", records[0])
        self.assertIn("\\x0d", records[0])
        self.assertIn("batch index 1", records[0])

    async def test_exception_traceback_is_sanitized_before_final_formatting(self):
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        handler.setFormatter(logging.Formatter("%(levelname)s:%(message)s"))
        logger = logging.getLogger("test.traceback-safety")
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.ERROR)
        adapter = _SanitizingLoggerAdapter(logger, {})

        try:
            raise RuntimeError("remote\r\nforged")
        except RuntimeError:
            adapter.error("sync failed", exc_info=True)

        formatted = output.getvalue()
        self.assertEqual(len(formatted.splitlines()), 1)
        self.assertIn("\\x0d\\x0a", formatted)

    async def test_exc_info_accepts_true_tuple_and_exception_instance(self):
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        logger = logging.getLogger("test.exc-info-forms")
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.ERROR)
        adapter = _SanitizingLoggerAdapter(logger, {})

        try:
            raise RuntimeError("operational failure")
        except RuntimeError as error:
            exc_tuple = (type(error), error, error.__traceback__)
            adapter.error("true", exc_info=True)
            adapter.error("tuple", exc_info=exc_tuple)
            adapter.error("instance", exc_info=error)

        formatted = output.getvalue()
        self.assertEqual(formatted.count("RuntimeError: operational failure"), 3)

    async def test_positional_log_arguments_and_unicode_separators_are_sanitized(self):
        records = []

        class _Handler(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        logger = logging.getLogger("test.positional-log-safety")
        logger.handlers = [_Handler()]
        logger.propagate = False
        logger.setLevel(logging.WARNING)
        adapter = _SanitizingLoggerAdapter(logger, {})

        adapter.warning("remote=%s", "first\r\nsecond\u2028third\u2029fourth")

        self.assertEqual(records, ["remote=first\\x0d\\x0asecond\\u2028third\\u2029fourth"])

    async def test_connect_restarts_worker_to_discover_stored_pending_work(self):
        controller = Controller.__new__(Controller)
        controller._shutdown_event = asyncio.Event()
        controller._shutdown_event.set()
        controller._hard_reset_event = asyncio.Event()
        controller._failure_count_lock = asyncio.Lock()
        controller._user_sync_failure_count = 3
        controller._task_lock = asyncio.Lock()
        controller._tasks = []
        controller._health_lock = asyncio.Lock()
        controller._version_lock = asyncio.Lock()
        controller._health = 0
        controller._node_version = ""
        controller._core_version = ""
        controller._work_available = asyncio.Event()
        controller._ensure_sync_worker_running = AsyncMock()

        await controller.connect("0.2.0", "1.0.0")

        self.assertTrue(controller._work_available.is_set())
        controller._ensure_sync_worker_running.assert_awaited_once()


class SharedStoreDisconnectTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _controller(store, worker_id):
        controller = Controller.__new__(Controller)
        controller.node_id = "node-1"
        controller.worker_id = worker_id
        controller._user_sync_store = store
        controller._shutdown_event = asyncio.Event()
        controller._task_lock = asyncio.Lock()
        controller._tasks = []
        controller._sync_worker_lock = asyncio.Lock()
        controller._sync_worker_task = None
        controller._work_available = asyncio.Event()
        controller._health_lock = asyncio.Lock()
        controller._version_lock = asyncio.Lock()
        controller._health = Health.HEALTHY
        controller._node_version = "0.2.0"
        controller._core_version = "1.0.0"
        controller._sync_lease_seconds = 30
        controller._worker_idle_timeout = 1
        controller._sync_poll_interval = 0
        controller._internal_timeout = 0.1
        controller._failure_count_lock = asyncio.Lock()
        controller._user_sync_failure_count = 0
        controller._hard_reset_threshold = 5
        controller._hard_reset_event = asyncio.Event()
        controller.name = worker_id
        controller.logger = _SanitizingLoggerAdapter(logging.getLogger("test.shared-store"), {})
        return controller

    @staticmethod
    async def _wait_until(predicate, timeout=0.2):
        async def poll():
            while not predicate():
                await asyncio.sleep(0.001)

        await asyncio.wait_for(poll(), timeout=timeout)

    async def test_disconnect_preserves_pending_work_for_second_controller(self):
        store = InMemoryUserSyncStore()
        first = self._controller(store, "worker-1")
        second = self._controller(store, "worker-2")
        await store.enqueue_users("node-1", [User(email="pending@example.com")])

        await asyncio.wait_for(first.disconnect(), timeout=1.0)
        claimed = await second._claim_pending_users()

        self.assertEqual([item.user.email for item in claimed], ["pending@example.com"])

    async def test_enqueue_during_empty_claim_does_not_lose_wakeup(self):
        controller = self._controller(InMemoryUserSyncStore(), "worker-1")
        claim_observed_empty = asyncio.Event()
        release_claim = asyncio.Event()

        async def claim_users(*_args, **_kwargs):
            claim_observed_empty.set()
            await release_claim.wait()
            return []

        controller._user_sync_store.claim_users = claim_users
        claim_task = asyncio.create_task(controller._claim_pending_users())
        await asyncio.wait_for(claim_observed_empty.wait(), timeout=0.1)

        # Model a local enqueue after the store observed an empty queue but
        # before claim_users() returns to the controller.
        controller._work_available.set()
        release_claim.set()

        self.assertEqual(await asyncio.wait_for(claim_task, timeout=0.1), [])
        self.assertTrue(controller._work_available.is_set())

    async def test_enqueue_racing_failed_claim_is_retried_by_existing_worker(self):
        store = InMemoryUserSyncStore()
        controller = self._controller(store, "worker-1")
        controller._supports_chunked_sync = AsyncMock(return_value=(False, "0.1.0"))
        first_claim_started = asyncio.Event()
        release_first_claim = asyncio.Event()
        processed = asyncio.Event()
        original_claim = store.claim_users
        claim_count = 0

        async def fail_first_claim(*args, **kwargs):
            nonlocal claim_count
            claim_count += 1
            if claim_count == 1:
                first_claim_started.set()
                await release_first_claim.wait()
                raise RuntimeError("transient store failure")
            return await original_claim(*args, **kwargs)

        async def successful_sync(users):
            processed.set()
            return []

        store.claim_users = fail_first_claim
        controller._sync_batch_users = successful_sync
        controller._work_available.set()

        with patch("PasarGuardNodeBridge.controller.INITIAL_CLAIM_RETRY_DELAY", 0.01):
            await controller._ensure_sync_worker_running()
            worker = controller._sync_worker_task
            await asyncio.wait_for(first_claim_started.wait(), timeout=0.2)

            # ensure_sync_worker_running observes the still-running first worker
            # while this update sets the wake event and enters the shared store.
            await controller.update_user(User(email="pending@example.com"))
            self.assertIs(controller._sync_worker_task, worker)
            release_first_claim.set()

            await asyncio.wait_for(processed.wait(), timeout=0.2)

        self.assertGreaterEqual(claim_count, 2)
        self.assertIs(controller._sync_worker_task, worker)
        await asyncio.wait_for(controller.disconnect(), timeout=0.2)
        self.assertIsNone(controller._sync_worker_task)
        self.assertTrue(worker.done())

    async def test_enqueue_before_idle_retirement_lock_keeps_current_worker(self):
        store = InMemoryUserSyncStore()
        controller = self._controller(store, "worker-1")
        controller._supports_chunked_sync = AsyncMock(return_value=(False, "0.1.0"))
        controller._worker_idle_timeout = 0.01
        processed = asyncio.Event()

        async def successful_sync(users):
            processed.set()
            return []

        controller._sync_batch_users = successful_sync
        await controller._ensure_sync_worker_running()
        worker = controller._sync_worker_task
        await controller._sync_worker_lock.acquire()
        update_task = None
        try:
            # Hold retirement at its lock boundary until enqueue has published
            # both the stored user and the wake event.
            await asyncio.sleep(0.02)
            update_task = asyncio.create_task(controller.update_user(User(email="pending@example.com")))
            await self._wait_until(controller._work_available.is_set)
        finally:
            controller._sync_worker_lock.release()

        try:
            await asyncio.wait_for(update_task, timeout=0.2)
            await asyncio.wait_for(processed.wait(), timeout=0.2)
            self.assertIs(controller._sync_worker_task, worker)
        finally:
            await asyncio.wait_for(controller.disconnect(), timeout=0.2)

        self.assertTrue(worker.done())
        self.assertIsNone(controller._sync_worker_task)

    async def test_enqueue_after_idle_retirement_clear_spawns_replacement(self):
        enqueue_started = asyncio.Event()
        release_enqueue = asyncio.Event()

        class BlockingEnqueueStore(InMemoryUserSyncStore):
            async def enqueue_users(self, node_id, users):
                enqueue_started.set()
                await release_enqueue.wait()
                await super().enqueue_users(node_id, users)

        store = BlockingEnqueueStore()
        controller = self._controller(store, "worker-1")
        controller._supports_chunked_sync = AsyncMock(return_value=(False, "0.1.0"))
        controller._worker_idle_timeout = 0.01
        processed = asyncio.Event()

        async def successful_sync(users):
            processed.set()
            return []

        controller._sync_batch_users = successful_sync
        await controller._ensure_sync_worker_running()
        retiring_worker = controller._sync_worker_task
        update_task = asyncio.create_task(controller.update_user(User(email="pending@example.com")))
        try:
            await asyncio.wait_for(enqueue_started.wait(), timeout=0.2)
            # The old worker has already captured the short timeout. Give the
            # replacement a generous timeout before releasing the enqueue.
            controller._worker_idle_timeout = 1.0
            await self._wait_until(lambda: controller._sync_worker_task is None)
            release_enqueue.set()
            await asyncio.wait_for(update_task, timeout=0.2)
            replacement = controller._sync_worker_task
            self.assertIsNotNone(replacement)
            self.assertIsNot(replacement, retiring_worker)
            await asyncio.wait_for(processed.wait(), timeout=0.2)
            await asyncio.wait_for(retiring_worker, timeout=0.2)
            self.assertIs(controller._sync_worker_task, replacement)
        finally:
            release_enqueue.set()
            if not update_task.done():
                update_task.cancel()
                await asyncio.gather(update_task, return_exceptions=True)
            await asyncio.wait_for(controller.disconnect(), timeout=0.2)

        self.assertTrue(retiring_worker.done())
        self.assertTrue(replacement.done())
        self.assertIsNone(controller._sync_worker_task)

    async def test_idle_retirement_boundary_100x_never_strands_enqueued_work(self):
        workers = []
        for iteration in range(100):
            store = InMemoryUserSyncStore()
            controller = self._controller(store, f"worker-{iteration}")
            controller._supports_chunked_sync = AsyncMock(return_value=(False, "0.1.0"))
            controller._worker_idle_timeout = 0.005
            processed = asyncio.Event()

            async def successful_sync(users, processed_event=processed):
                processed_event.set()
                return []

            controller._sync_batch_users = successful_sync
            await controller._ensure_sync_worker_running()
            original_worker = controller._sync_worker_task
            workers.append(original_worker)
            try:
                if iteration % 2 == 0:
                    # Enqueue wins: the current worker must survive retirement.
                    await controller._sync_worker_lock.acquire()
                    try:
                        await asyncio.sleep(0.01)
                        update_task = asyncio.create_task(
                            controller.update_user(User(email=f"pending-{iteration}@example.com"))
                        )
                        await self._wait_until(controller._work_available.is_set)
                    finally:
                        controller._sync_worker_lock.release()
                    await asyncio.wait_for(update_task, timeout=0.5)
                    self.assertIs(controller._sync_worker_task, original_worker)
                else:
                    # Retirement wins: clearing the reference must let enqueue
                    # publish a replacement instead of observing a dying task.
                    await self._wait_until(lambda current=controller: current._sync_worker_task is None)
                    controller._worker_idle_timeout = 1.0
                    await asyncio.wait_for(
                        controller.update_user(User(email=f"pending-{iteration}@example.com")), timeout=0.5
                    )
                    self.assertIsNot(controller._sync_worker_task, original_worker)

                try:
                    await asyncio.wait_for(processed.wait(), timeout=0.5)
                except TimeoutError:
                    self.fail(f"queued work was stranded at retirement iteration {iteration}")
            finally:
                if controller._sync_worker_lock.locked():
                    controller._sync_worker_lock.release()
                await asyncio.wait_for(controller.disconnect(), timeout=0.5)

            self.assertIsNone(controller._sync_worker_task)

        self.assertTrue(all(worker.done() for worker in workers))

    async def test_persistent_claim_failure_backs_off_and_disconnect_cleans_worker(self):
        class FailingClaimStore(InMemoryUserSyncStore):
            def __init__(self):
                super().__init__()
                self.claim_times = []
                self.third_claim = asyncio.Event()

            async def claim_users(self, *args, **kwargs):
                self.claim_times.append(asyncio.get_running_loop().time())
                if len(self.claim_times) == 3:
                    self.third_claim.set()
                raise RuntimeError("store unavailable")

        store = FailingClaimStore()
        controller = self._controller(store, "worker-1")
        controller._supports_chunked_sync = AsyncMock(return_value=(False, "0.1.0"))
        controller._work_available.set()

        with (
            patch("PasarGuardNodeBridge.controller.INITIAL_CLAIM_RETRY_DELAY", 0.01),
            patch("PasarGuardNodeBridge.controller.MAX_CLAIM_RETRY_DELAY", 0.02),
        ):
            await controller._ensure_sync_worker_running()
            worker = controller._sync_worker_task
            await asyncio.wait_for(store.third_claim.wait(), timeout=0.2)

            self.assertEqual(len(store.claim_times), 3)
            self.assertTrue(all(b > a for a, b in zip(store.claim_times, store.claim_times[1:])))
            self.assertGreaterEqual(store.claim_times[-1] - store.claim_times[0], 0.015)
            self.assertIs(controller._sync_worker_task, worker)

            await asyncio.wait_for(controller.disconnect(), timeout=0.2)

        self.assertIsNone(controller._sync_worker_task)
        self.assertTrue(worker.done())
        claim_count = len(store.claim_times)
        await asyncio.sleep(0.025)
        self.assertEqual(len(store.claim_times), claim_count)

    async def test_invalid_health_stops_claim_retry_without_restart(self):
        store = InMemoryUserSyncStore()
        controller = self._controller(store, "worker-1")
        controller._supports_chunked_sync = AsyncMock(return_value=(False, "0.1.0"))
        claim_failed = asyncio.Event()
        claim_count = 0

        async def fail_claim(*_args, **_kwargs):
            nonlocal claim_count
            claim_count += 1
            claim_failed.set()
            raise RuntimeError("store unavailable")

        store.claim_users = fail_claim
        controller._work_available.set()

        with patch("PasarGuardNodeBridge.controller.INITIAL_CLAIM_RETRY_DELAY", 0.01):
            await controller._ensure_sync_worker_running()
            worker = controller._sync_worker_task
            await asyncio.wait_for(claim_failed.wait(), timeout=0.2)
            await controller.set_health(Health.INVALID)
            await asyncio.wait_for(worker, timeout=0.2)
            await asyncio.sleep(0)

        self.assertEqual(claim_count, 1)
        self.assertIsNone(controller._sync_worker_task)

    async def test_zero_claim_deadline_uses_positive_backoff_when_polling_disabled(self):
        controller = self._controller(InMemoryUserSyncStore(), "worker-1")
        controller._sync_poll_interval = 0
        observed_timeouts = []

        async def capture_wait(awaitable, *, timeout):
            awaitable.close()
            observed_timeouts.append(timeout)
            raise asyncio.TimeoutError

        with patch("PasarGuardNodeBridge.controller.asyncio.wait_for", side_effect=capture_wait):
            await controller._wait_for_claim_recheck(0.0)

        self.assertEqual(len(observed_timeouts), 1)
        self.assertGreater(observed_timeouts[0], 0.0)
        self.assertTrue(controller._work_available.is_set())

    async def test_tiny_positive_claim_deadline_uses_minimum_backoff(self):
        controller = self._controller(InMemoryUserSyncStore(), "worker-1")
        controller._sync_poll_interval = 0
        observed_timeouts = []

        async def capture_wait(awaitable, *, timeout):
            awaitable.close()
            observed_timeouts.append(timeout)
            raise asyncio.TimeoutError

        with patch("PasarGuardNodeBridge.controller.asyncio.wait_for", side_effect=capture_wait):
            await controller._wait_for_claim_recheck(1e-9)

        self.assertEqual(observed_timeouts, [0.01])

    async def test_lease_aware_empty_store_exits_without_poll_interval_delay(self):
        controller = self._controller(InMemoryUserSyncStore(), "worker-1")
        controller._supports_chunked_sync = AsyncMock(return_value=(False, "0.1.0"))
        controller._worker_idle_timeout = 0.02
        controller._sync_poll_interval = 1.0
        controller._work_available.set()
        loop = asyncio.get_running_loop()
        started = loop.time()

        await asyncio.wait_for(controller._sync_worker(), timeout=0.2)

        self.assertLess(loop.time() - started, 0.15)

    async def test_zero_deadline_worker_cancels_without_hot_loop_or_task_leak(self):
        class DueElsewhereStore(InMemoryUserSyncStore):
            def __init__(self):
                super().__init__()
                self.claim_count = 0

            async def claim_users(self, *args, **kwargs):
                self.claim_count += 1
                return []

            async def next_claim_delay(self, node_id):
                return 0.0

        store = DueElsewhereStore()
        controller = self._controller(store, "worker-1")
        controller._supports_chunked_sync = AsyncMock(return_value=(False, "0.1.0"))
        controller._worker_idle_timeout = 1.0
        controller._sync_poll_interval = 0
        controller._work_available.set()
        controller._sync_worker_task = asyncio.create_task(controller._sync_worker())
        worker = controller._sync_worker_task

        await asyncio.sleep(0.05)

        self.assertLessEqual(store.claim_count, 7)
        await asyncio.wait_for(controller.disconnect(), timeout=0.5)
        self.assertIsNone(controller._sync_worker_task)
        self.assertTrue(worker.done())

    async def test_cancel_after_claim_requeues_immediately_for_second_controller(self):
        store = InMemoryUserSyncStore()
        first = self._controller(store, "worker-1")
        second = self._controller(store, "worker-2")
        first._supports_chunked_sync = AsyncMock(return_value=(False, "0.1.0"))
        sync_started = asyncio.Event()

        async def blocking_sync(users):
            sync_started.set()
            await asyncio.Event().wait()

        first._sync_batch_users = blocking_sync
        await store.enqueue_users("node-1", [User(email="pending@example.com")])
        first._work_available.set()
        first._sync_worker_task = asyncio.create_task(first._sync_worker())
        await asyncio.wait_for(sync_started.wait(), timeout=0.2)

        await asyncio.wait_for(first.disconnect(), timeout=1.0)
        claimed = await second._claim_pending_users()

        self.assertEqual([item.user.email for item in claimed], ["pending@example.com"])

    async def test_flush_pending_users_remains_explicit_destructive_clear(self):
        store = InMemoryUserSyncStore()
        controller = self._controller(store, "worker-1")
        await store.enqueue_users("node-1", [User(email="pending@example.com")])

        await controller.flush_pending_users()

        self.assertEqual(await controller._claim_pending_users(), [])

    async def test_outer_worker_failure_requeues_claimed_users(self):
        store = InMemoryUserSyncStore()
        controller = self._controller(store, "worker-1")
        controller._supports_chunked_sync = AsyncMock(return_value=(False, "0.1.0"))
        controller._sync_batch_users = AsyncMock(return_value=[])
        controller._ack_claimed_users = AsyncMock(side_effect=RuntimeError("ack failed"))
        await store.enqueue_users("node-1", [User(email="pending@example.com")])
        controller._work_available.set()

        async def stop_after_recovery(_delay):
            controller._shutdown_event.set()

        with patch("PasarGuardNodeBridge.controller.asyncio.sleep", side_effect=stop_after_recovery):
            await controller._sync_worker()

        recovered = await store.claim_users("node-1", "worker-2", limit=10, lease_seconds=30)
        self.assertEqual([item.user.email for item in recovered], ["pending@example.com"])

    async def test_partial_ack_then_failed_requeue_recovers_only_failed_claim(self):
        store = InMemoryUserSyncStore()
        controller = self._controller(store, "worker-1")
        controller._supports_chunked_sync = AsyncMock(return_value=(False, "0.1.0"))
        succeeded = User(email="ok@example.com")
        failed = User(email="failed@example.com")
        controller._sync_batch_users = AsyncMock(return_value=[failed])
        original_requeue = controller._requeue_claimed_users
        requeue_calls = []

        async def fail_once_then_requeue(claimed_users):
            requeue_calls.append([item.user.email for item in claimed_users])
            if len(requeue_calls) <= 2:
                raise RuntimeError("store unavailable")
            await original_requeue(claimed_users)

        controller._requeue_claimed_users = fail_once_then_requeue
        await store.enqueue_users("node-1", [succeeded, failed])
        controller._work_available.set()

        await controller._sync_worker()

        recovered = await store.claim_users("node-1", "worker-2", limit=10, lease_seconds=30)
        self.assertEqual(
            requeue_calls,
            [["failed@example.com"], ["failed@example.com"], ["failed@example.com"]],
        )
        self.assertEqual([item.user.email for item in recovered], ["failed@example.com"])

    async def test_second_worker_wakes_after_failed_requeue_lease_expires(self):
        store = InMemoryUserSyncStore()
        first = self._controller(store, "worker-1")
        second = self._controller(store, "worker-2")
        first._sync_lease_seconds = 0.08
        second._worker_idle_timeout = 0.01
        second._sync_poll_interval = 0
        first._supports_chunked_sync = AsyncMock(return_value=(False, "0.1.0"))
        second._supports_chunked_sync = AsyncMock(return_value=(False, "0.1.0"))
        sync_started = asyncio.Event()
        second_processed = asyncio.Event()

        async def blocking_sync(users):
            sync_started.set()
            await asyncio.Event().wait()

        async def successful_sync(users):
            second_processed.set()
            return []

        first._sync_batch_users = blocking_sync
        second._sync_batch_users = successful_sync
        first._requeue_claimed_users = AsyncMock(side_effect=RuntimeError("store unavailable"))
        await store.enqueue_users("node-1", [User(email="pending@example.com")])
        first._work_available.set()
        first._sync_worker_task = asyncio.create_task(first._sync_worker())
        await asyncio.wait_for(sync_started.wait(), timeout=0.2)

        await asyncio.wait_for(first.disconnect(), timeout=0.2)

        self.assertIsNone(first._sync_worker_task)
        second._work_available.set()
        second._sync_worker_task = asyncio.create_task(second._sync_worker())
        second_worker = second._sync_worker_task

        await asyncio.sleep(0.02)
        self.assertFalse(second_processed.is_set())
        self.assertFalse(second_worker.done())

        await asyncio.wait_for(second_processed.wait(), timeout=0.5)
        for _ in range(20):
            if await store.next_claim_delay("node-1") is None:
                break
            await asyncio.sleep(0.005)
        self.assertIsNone(await store.next_claim_delay("node-1"))

        await asyncio.wait_for(second.disconnect(), timeout=0.2)
        self.assertIsNone(second._sync_worker_task)
        self.assertTrue(second_worker.done())

    async def test_outer_worker_failure_retries_failed_requeue(self):
        store = InMemoryUserSyncStore()
        controller = self._controller(store, "worker-1")
        controller._supports_chunked_sync = AsyncMock(return_value=(False, "0.1.0"))
        user = User(email="pending@example.com")
        controller._sync_batch_users = AsyncMock(return_value=[user])
        original_requeue = controller._requeue_claimed_users
        requeue_calls = 0

        async def fail_once_then_requeue(claimed_users):
            nonlocal requeue_calls
            requeue_calls += 1
            if requeue_calls == 1:
                raise RuntimeError("store unavailable")
            await original_requeue(claimed_users)

        controller._requeue_claimed_users = fail_once_then_requeue
        await store.enqueue_users("node-1", [user])
        controller._work_available.set()

        async def stop_after_recovery(_delay):
            controller._shutdown_event.set()

        with patch("PasarGuardNodeBridge.controller.asyncio.sleep", side_effect=stop_after_recovery):
            await controller._sync_worker()

        recovered = await store.claim_users("node-1", "worker-2", limit=10, lease_seconds=30)
        self.assertEqual(requeue_calls, 2)
        self.assertEqual([item.user.email for item in recovered], ["pending@example.com"])


if __name__ == "__main__":
    unittest.main()
