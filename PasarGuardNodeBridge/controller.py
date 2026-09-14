import asyncio
import logging
import math
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from enum import IntEnum
from json import JSONDecodeError
from uuid import UUID

import aiohttp
from packaging.version import InvalidVersion, Version

from PasarGuardNodeBridge.aiohttp_compat import (
    BufferedResponse,
    BufferedStatusError,
    LazyClientSession,
    buffer_response,
    make_timeout,
)
from PasarGuardNodeBridge.common.service_pb2 import User
from PasarGuardNodeBridge.proxy import parse_proxy_url
from PasarGuardNodeBridge.storage import (
    ClaimedUser,
    LifecycleLease,
    LifecycleOperation,
    LifecycleStatus,
    NodeLifecycleCoordinatorProtocol,
    NodeLifecycleState,
    UserSyncStoreProtocol,
    get_default_lifecycle_coordinator,
    get_default_user_sync_store,
)

# Default timeout configuration (module-level constants)
DEFAULT_API_TIMEOUT = 10  # Default timeout for public API methods
DEFAULT_INTERNAL_TIMEOUT = 15  # Default timeout for internal gRPC/HTTP operations


class NodeAPIError(Exception):
    def __init__(self, code, detail):
        self.code = code
        self.detail = detail

    def __str__(self):
        return f"NodeAPIError(code={self.code}, detail={self.detail})"


class Health(IntEnum):
    NOT_CONNECTED = 0
    BROKEN = 1
    HEALTHY = 2
    INVALID = 3


class FullSyncHold:
    """Ownership check for a renewable full-sync lease."""

    def __init__(self, owner: asyncio.Task, until: float):
        self.owner = owner
        self.until = until
        self.lost = False

    def check(self) -> None:
        if self.lost or asyncio.get_running_loop().time() >= self.until:
            raise NodeAPIError(409, "Full user sync lease was lost")


class Controller:
    def __init__(
        self,
        server_ca: str,
        api_key: str,
        service_url: str,
        name: str = "default",
        extra: dict | None = None,
        logger: logging.Logger | None = None,
        default_timeout: int = DEFAULT_API_TIMEOUT,
        internal_timeout: int = DEFAULT_INTERNAL_TIMEOUT,
        proxy: str | None = None,
        node_id: str | None = None,
        user_sync_store: UserSyncStoreProtocol | None = None,
        worker_id: str | None = None,
        sync_poll_interval: float = 1.0,
        sync_lease_seconds: float = 30.0,
        lifecycle_coordinator: NodeLifecycleCoordinatorProtocol | None = None,
        lifecycle_lease_seconds: float = 60.0,
        sync_batch_size: int = 100,
        sync_chunk_size: int = 100,
        user_sync_resolver: Callable[[str, list[str]], Awaitable[list[User]]] | None = None,
    ):
        for option, value in (("sync_poll_interval", sync_poll_interval), ("sync_lease_seconds", sync_lease_seconds)):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{option} must be finite and positive")
        for option, value in (("sync_batch_size", sync_batch_size), ("sync_chunk_size", sync_chunk_size)):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{option} must be a positive integer")
        self._sync_batch_size = sync_batch_size
        self._sync_chunk_size = sync_chunk_size
        self._user_sync_resolver = user_sync_resolver
        self._claim_generation = 0
        self._recovery_until = 0.0
        self.name = name
        self.node_id = node_id or service_url
        self.worker_id = worker_id or f"{self.node_id}:{id(self)}"
        if extra is None:
            extra = {}
        if logger is None:
            logger = logging.getLogger(self.name)
            logger.setLevel(logging.INFO)
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
            logger.addHandler(handler)
        self.logger = logger

        # Timeout configuration
        self._default_timeout = default_timeout
        self._internal_timeout = internal_timeout
        try:
            self.api_key = UUID(api_key)

            self.h2_ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
            self.h2_ctx.load_verify_locations(cadata=server_ca)
            self.h2_ctx.check_hostname = True
            self.h2_ctx.set_alpn_protocols(["h2"])

            self.http1_ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
            self.http1_ctx.load_verify_locations(cadata=server_ca)
            self.http1_ctx.check_hostname = True
            self.http1_ctx.set_alpn_protocols(["http/1.1"])

            # Backward-compatible alias for existing transport code that still expects `self.ctx`.
            self.ctx = self.h2_ctx

        except ssl.SSLError as e:
            raise NodeAPIError(-1, f"SSL initialization failed: {e!s}")

        except (ValueError, TypeError) as e:
            raise NodeAPIError(-2, f"Invalid API key format: {e!s}")

        try:
            self._proxy = parse_proxy_url(proxy)
        except ValueError as e:
            raise NodeAPIError(-6, f"Invalid proxy format: {e!s}") from e

        self._health = Health.NOT_CONNECTED
        self._tasks: list[asyncio.Task] = []
        self._node_version = ""
        self._core_version = ""
        self._extra = extra

        # Lazy worker sync mechanism
        self._user_sync_store = user_sync_store if user_sync_store is not None else get_default_user_sync_store()
        self._sync_worker_task: asyncio.Task | None = None
        self._work_available = asyncio.Event()
        self._worker_idle_timeout = 5.0  # seconds before worker exits
        self._sync_poll_interval = sync_poll_interval
        self._sync_lease_seconds = sync_lease_seconds
        self._lifecycle_coordinator = (
            lifecycle_coordinator if lifecycle_coordinator is not None else get_default_lifecycle_coordinator()
        )
        self._lifecycle_lease_seconds = lifecycle_lease_seconds
        self._lifecycle_heartbeat_tasks: dict[str, asyncio.Task] = {}

        # Hard reset mechanism for critical failures
        self._hard_reset_event = asyncio.Event()
        self._user_sync_failure_count = 0
        self._hard_reset_threshold = 5
        self._failure_count_lock = asyncio.Lock()  # Only for incrementing counters

        # Separate locks for different resources to reduce contention
        self._health_lock = asyncio.Lock()
        self._sync_worker_lock = asyncio.Lock()
        self._version_lock = asyncio.Lock()
        self._task_lock = asyncio.Lock()

        self._shutdown_event = asyncio.Event()

        self._json_client = LazyClientSession(
            ssl_context=self.http1_ctx,
            headers={"Content-Type": "application/json", "x-api-key": api_key},
            base_url=service_url,
            timeout=make_timeout(default_timeout),
            connector_factory=None if self._proxy is None else self._proxy.aiohttp_connector_factory,
            proxy=None if self._proxy is None else self._proxy.aiohttp_proxy_url,
            proxy_auth=None if self._proxy is None else self._proxy.aiohttp_proxy_auth,
        )

    async def set_health(self, health: Health):
        async with self._health_lock:
            # INVALID is permanent - once set, it cannot be changed (instance is being deleted)
            if self._health is Health.INVALID:
                return
            self._health = health

    async def get_health(self) -> Health:
        async with self._health_lock:
            return self._health

    def requires_hard_reset(self) -> bool:
        """Check if hard reset is required due to critical failures.

        This is a synchronous, non-blocking check using Event.is_set().
        """
        return self._hard_reset_event.is_set()

    async def _increment_user_sync_failure(self):
        """Increment user sync failure counter and check if hard reset is needed."""
        async with self._failure_count_lock:
            self._user_sync_failure_count += 1
            if self._user_sync_failure_count >= self._hard_reset_threshold and not self._hard_reset_event.is_set():
                self._hard_reset_event.set()
                self.logger.critical(
                    f"[{self.name}] HARD RESET REQUIRED: User sync failed {self._user_sync_failure_count} times in a row"
                )

    async def _reset_user_sync_failure_count(self):
        """Reset user sync failure counter on successful sync and clear hard reset event."""
        async with self._failure_count_lock:
            old_count = self._user_sync_failure_count
            self._user_sync_failure_count = 0
            # Clear hard reset event if it was set
            if self._hard_reset_event.is_set():
                self._hard_reset_event.clear()
                if old_count > 0:
                    self.logger.info(
                        f"[{self.name}] User sync recovered after {old_count} failures, cleared hard reset event"
                    )

    async def update_user(self, user: User):
        """Queue a user for sync. Automatically deduplicates by email."""
        await self._user_sync_store.enqueue_users(self.node_id, [user])
        self._work_available.set()

        # Ensure worker is running to process the update
        await self._ensure_sync_worker_running()

    async def update_users(self, users: list[User]):
        """Queue multiple users for sync. Automatically deduplicates by email."""
        if not users:
            return

        await self._user_sync_store.enqueue_users(self.node_id, users)
        self._work_available.set()

        # Ensure worker is running to process the updates
        await self._ensure_sync_worker_running()

    async def _try_recover_health_after_sync(
        self, was_broken: bool, was_invalid: bool
    ) -> tuple[float | None, float | None]:
        """
        Attempt to recover node health from BROKEN or INVALID to HEALTHY after successful sync.

        Args:
            was_broken: Whether the node was BROKEN before sync
            was_invalid: Whether the node was INVALID before sync

        Returns:
            Tuple of (retry_delay, sync_retry_delay) - (10.0, 1.0) if recovery succeeded,
            (None, None) if no recovery needed or recovery failed
        """
        if not (was_broken or was_invalid):
            return None, None  # No recovery needed

        current_health = await self.get_health()
        if current_health not in (Health.BROKEN, Health.INVALID):
            return None, None  # Already recovered

        try:
            # Verify node is actually healthy before updating
            await self.get_backend_stats()
            await self.set_health(Health.HEALTHY)
            health_status = "BROKEN" if was_broken else "INVALID"
            self.logger.info(f"[{self.name}] Sync succeeded while {health_status}, node health updated to HEALTHY")
            # Return reset delays
            return 10.0, 1.0
        except Exception as e:
            # Node still not responding, keep current health status
            error_type = type(e).__name__
            self.logger.debug(
                f"[{self.name}] Sync succeeded but health check failed, keeping {current_health.name} | "
                f"Error: {error_type} - {e!s}"
            )
            return None, None  # Keep current delays

    async def flush_pending_users(self):
        """Clear all pending users without syncing them."""
        await self._user_sync_store.clear(self.node_id)

    async def node_version(self) -> str:
        async with self._version_lock:
            return self._node_version

    async def core_version(self) -> str:
        async with self._version_lock:
            return self._core_version

    async def get_versions(self) -> tuple[str, str]:
        """Get both node and core versions atomically.

        Returns:
            tuple[str, str]: (node_version, core_version)
        """
        async with self._version_lock:
            return self._node_version, self._core_version

    async def get_extra(self) -> dict:
        async with self._version_lock:
            return self._extra

    @staticmethod
    def _parse_version(version: str) -> Version | None:
        """Parse semver-like strings into a packaging Version for comparison."""
        if not version:
            return None

        # Drop build metadata first, then pre-release suffixes
        cleaned = version.split("+", 1)[0]
        cleaned = cleaned.split("-", 1)[0]

        try:
            return Version(cleaned)
        except InvalidVersion:
            return None

    @classmethod
    def _is_version_at_least(cls, version: str, minimum: str) -> bool:
        current = cls._parse_version(version)
        target = cls._parse_version(minimum)

        if current is None or target is None:
            return False

        return current >= target

    async def _supports_chunked_sync(self) -> tuple[bool, str]:
        """Check if the connected node supports chunked sync (>= v0.2.0)."""
        node_version = await self.node_version()
        return self._is_version_at_least(node_version, "0.2.0"), node_version

    async def _heartbeat_lifecycle_lease(self, lease: LifecycleLease) -> None:
        interval = max(self._lifecycle_lease_seconds / 3, 0.01)
        try:
            while True:
                await asyncio.sleep(interval)
                await self._lifecycle_coordinator.heartbeat(lease)
        except asyncio.CancelledError:
            pass

    async def _acquire_lifecycle_lease(self, operation: LifecycleOperation) -> LifecycleLease:
        lease = await self._lifecycle_coordinator.try_acquire(
            self.node_id, self.worker_id, operation, self._lifecycle_lease_seconds
        )
        if lease is None:
            raise NodeAPIError(409, f"Node lifecycle operation already in progress for {self.node_id}")
        self._lifecycle_heartbeat_tasks[lease.token] = asyncio.create_task(self._heartbeat_lifecycle_lease(lease))
        return lease

    async def _release_lifecycle_lease(
        self,
        lease: LifecycleLease,
        observed: LifecycleStatus | None = None,
        desired: LifecycleStatus | None = None,
        node_version: str = "",
        core_version: str = "",
    ) -> None:
        heartbeat = self._lifecycle_heartbeat_tasks.pop(lease.token, None)
        if heartbeat is not None:
            heartbeat.cancel()
            await heartbeat

        if observed is None:
            await self._lifecycle_coordinator.release(lease)
            return

        state = NodeLifecycleState(
            desired=desired or observed,
            observed=observed,
            epoch=lease.epoch,
            operation=lease.operation,
            owner=lease.worker_id,
            node_version=node_version,
            core_version=core_version,
        )
        await self._lifecycle_coordinator.release(lease, state)

    async def get_lifecycle_state(self) -> NodeLifecycleState | None:
        return await self._lifecycle_coordinator.get_state(self.node_id)

    async def update_observed_lifecycle(self, observed: LifecycleStatus, expected_epoch: int | None = None) -> None:
        await self._lifecycle_coordinator.update_observed(self.node_id, observed, expected_epoch)

    async def connect(self, node_version: str, core_version: str, tasks: list | None = None):
        # Validate versions are not empty
        if not node_version or not core_version:
            raise NodeAPIError(-3, "Invalid version information from node")

        if tasks is None:
            tasks = []

        # Clear shutdown event first (no lock needed)
        self._shutdown_event.clear()

        # Reset hard reset event and failure counters
        self._hard_reset_event.clear()
        async with self._failure_count_lock:
            self._user_sync_failure_count = 0

        # Cleanup tasks with task lock
        async with self._task_lock:
            await self._cleanup_tasks()

        # Set health and versions atomically to prevent race condition
        async with self._health_lock, self._version_lock:
            self._node_version = node_version
            self._core_version = core_version
            if self._health is Health.INVALID:
                raise NodeAPIError(code=-4, detail="Invalid node")
            self._health = Health.HEALTHY

        # Create new tasks
        async with self._task_lock:
            for t in tasks:
                task = asyncio.create_task(t())
                self._tasks.append(task)

        self._recovery_until = asyncio.get_running_loop().time() + self._sync_lease_seconds
        await self.wake_sync_worker()

    async def disconnect(self):
        # Set shutdown event (no lock needed)
        self._shutdown_event.set()

        # Cleanup tasks
        async with self._task_lock:
            await self._cleanup_tasks()

        # Stop this worker; shared queued work survives disconnect
        await self._cleanup_sync_worker()

        # Clear versions and set health atomically to prevent race condition
        async with self._health_lock:
            async with self._version_lock:
                self._node_version = ""
                self._core_version = ""
            # Set health after versions are cleared
            if self._health is not Health.INVALID:
                self._health = Health.NOT_CONNECTED

    async def _cleanup_tasks(self):
        """Clean up all background tasks properly - must be called with task_lock held"""
        if self._tasks:
            for task in self._tasks:
                if not task.done():
                    task.cancel()

            try:
                results = await asyncio.wait_for(asyncio.gather(*self._tasks, return_exceptions=True), timeout=5.0)
                # Log any exceptions from tasks
                for i, result in enumerate(results):
                    if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                        error_type = type(result).__name__
                        self.logger.error(
                            f"[{self.name}] Task {i} raised exception during cleanup | Error: {error_type} - {result!s}"
                        )
            except TimeoutError:
                self.logger.warning(f"[{self.name}] Timeout waiting for {len(self._tasks)} tasks to cleanup")

            self._tasks.clear()

    async def _cleanup_sync_worker(self):
        """Stop this worker without deleting shared pending or leased work."""
        async with self._sync_worker_lock:
            task = self._sync_worker_task
            if task is not None and not task.done():
                task.cancel()
                with suppress(TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=2.0)
            self._sync_worker_task = None

    def is_shutting_down(self) -> bool:
        return self._shutdown_event.is_set()

    async def wake_sync_worker(self) -> None:
        """Notify this controller of work, including work enqueued by another process."""
        self._work_available.set()
        await self._ensure_sync_worker_running()

    async def _ensure_sync_worker_running(self):
        async with self._sync_worker_lock:
            if not self.is_shutting_down() and (self._sync_worker_task is None or self._sync_worker_task.done()):
                self._sync_worker_task = asyncio.create_task(self._sync_worker())

    async def _fence_state(self) -> tuple[bool, int]:
        state = getattr(self._user_sync_store, "fence_state", None)
        return await state(self.node_id) if callable(state) else (False, 0)

    async def _claim_pending_users(self, limit: int | None = None) -> list[ClaimedUser]:
        active, generation = await self._fence_state()
        if active:
            self._work_available.set()
            return []
        self._claim_generation = generation
        # Consume before reading: an enqueue concurrent with an empty result must
        # retain its wake. Keep draining after any bounded non-empty claim.
        self._work_available.clear()
        try:
            claimed = await self._user_sync_store.claim_users(
                self.node_id,
                self.worker_id,
                limit=min(limit or self._sync_batch_size, self._sync_batch_size),
                lease_seconds=self._sync_lease_seconds,
            )
            if claimed:
                self._work_available.set()
            else:
                has_pending = getattr(self._user_sync_store, "has_pending", None)
                if callable(has_pending):
                    if await has_pending(self.node_id):
                        self._work_available.set()
                elif asyncio.get_running_loop().time() < self._recovery_until:
                    # Legacy stores cannot report outstanding leases. Poll through
                    # the recovery window after reconnect or an uncertain claim.
                    self._work_available.set()
            return claimed
        except BaseException:
            self._work_available.set()
            raise

    async def _ack_claimed_users(self, claimed_users: list[ClaimedUser]):
        if not claimed_users:
            return
        active, generation = await self._fence_state()
        if active or generation != self._claim_generation:
            refresh = getattr(self._user_sync_store, "refresh_claimed", None)
            if callable(refresh):
                await refresh(self.node_id, claimed_users)
            else:
                await self._requeue_claimed_users(claimed_users)
            self._work_available.set()
            return
        unconfirmed = await self._user_sync_store.ack_users(self.node_id, [item.token for item in claimed_users])
        if unconfirmed:
            refresh = getattr(self._user_sync_store, "request_refresh", None)
            if not callable(refresh):
                raise RuntimeError("A store returning unconfirmed emails must implement request_refresh")
            await refresh(self.node_id, list(unconfirmed))
            self._work_available.set()

    async def _requeue_claimed_users(self, claimed_users: list[ClaimedUser]):
        if claimed_users:
            self._work_available.set()
            await self._user_sync_store.requeue_users(self.node_id, claimed_users)

    async def _deliver_claimed_users(self, claimed_users: list[ClaimedUser]) -> list[User]:
        users = [item.user for item in claimed_users]
        if self._user_sync_resolver is not None:
            current = {
                user.email: user
                for user in await self._user_sync_resolver(self.node_id, [user.email for user in users])
            }
            # Missing records cannot safely fall back to a stale queued payload.
            # Applications must return an explicit removal payload for deleted users.
            users = [current[user.email] for user in users]
        supports_chunked, _ = await self._supports_chunked_sync()
        if supports_chunked:
            return await self.sync_users_chunked(
                users, chunk_size=self._sync_chunk_size, flush_pending=False, timeout=self._internal_timeout
            )
        return await self._sync_batch_users(users)

    async def _sync_worker(self):
        """Drain bounded batches, retaining wakes and retrying failures with backoff."""
        retry_delay = 1.0
        claimed: list[ClaimedUser] = []
        loop = asyncio.get_running_loop()
        try:
            while not self.is_shutting_down():
                try:
                    await asyncio.wait_for(self._work_available.wait(), timeout=self._worker_idle_timeout)
                except TimeoutError:
                    # No await between this check and task exit: an enqueue either
                    # keeps this worker alive or sees a completed task and starts one.
                    if self._work_available.is_set():
                        continue
                    return
                try:
                    health = await self.get_health()
                    if health in (Health.NOT_CONNECTED, Health.INVALID):
                        return
                    if health == Health.BROKEN:
                        await asyncio.sleep(retry_delay)
                        retry_delay = min(retry_delay * 2, 30.0)
                        continue
                    # Store latency, resolver, transport lock and all stream chunks
                    # share one deadline ending before another worker can own the lease.
                    budget = self._sync_lease_seconds - min(1.0, self._sync_lease_seconds / 4)
                    async with asyncio.timeout(budget):
                        claimed = await self._claim_pending_users()
                        if claimed:
                            active, generation = await self._fence_state()
                            if active or generation != self._claim_generation:
                                # A snapshot began during the claim. Nothing has
                                # been sent, so defer without counting a failure.
                                await self._requeue_claimed_users(claimed)
                                claimed = []
                        failed = await self._deliver_claimed_users(claimed) if claimed else []
                    if not claimed:
                        await asyncio.sleep(self._sync_poll_interval)
                        continue
                    failed_emails = {user.email for user in failed}
                    async with asyncio.timeout(self._internal_timeout):
                        await self._ack_claimed_users(
                            [item for item in claimed if item.user.email not in failed_emails]
                        )
                        await self._requeue_claimed_users(
                            [item for item in claimed if item.user.email in failed_emails]
                        )
                    claimed = []
                    if failed:
                        await self._increment_user_sync_failure()
                        await asyncio.sleep(retry_delay)
                        retry_delay = min(retry_delay * 2, 30.0)
                    else:
                        await self._reset_user_sync_failure_count()
                        retry_delay = 1.0
                except Exception as exc:
                    self.logger.warning(f"[{self.name}] User sync failed; retrying: {type(exc).__name__}: {exc!s}")
                    self._work_available.set()
                    self._recovery_until = loop.time() + self._sync_lease_seconds
                    with suppress(Exception):
                        async with asyncio.timeout(self._internal_timeout):
                            await self._requeue_claimed_users(claimed)
                    claimed = []
                    await self._increment_user_sync_failure()
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 30.0)
        finally:
            if claimed:
                # Cancellation releases owned claims; if storage is unavailable,
                # their leases retain the work for the next worker to recover.
                with suppress(Exception):
                    async with asyncio.timeout(self._internal_timeout):
                        await self._requeue_claimed_users(claimed)
            if self._sync_worker_task is asyncio.current_task():
                self._sync_worker_task = None

    async def full_sync_in_progress(self) -> bool:
        return (await self._fence_state())[0]

    async def _renew_full_sync(self, token: str, lease_seconds: float, held: FullSyncHold) -> None:
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(lease_seconds / 3)
            started = loop.time()
            try:
                async with asyncio.timeout_at(held.until):
                    renewed = await self._user_sync_store.renew_full_sync(self.node_id, token, lease_seconds)
                if renewed:
                    held.until = started + lease_seconds
                    continue
            except Exception as exc:
                # Fail closed when the store cannot confirm ownership. The owner
                # must stop before a competing snapshot can acquire this fence.
                self.logger.warning(f"[{self.name}] Full-sync lease renewal failed: {type(exc).__name__}")
            held.lost = True
            held.owner.cancel()
            return

    @asynccontextmanager
    async def full_sync_fence(self, lease_seconds: float = 120.0) -> AsyncIterator[FullSyncHold]:
        """Pause shared delta delivery while a fresh snapshot is read and applied.

        Requires SnapshotUserSyncStoreProtocol; no unsafe process-local fallback
        is used for an external store missing shared coordination.
        """
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be finite and positive")
        store = self._user_sync_store
        methods = (
            "begin_full_sync",
            "renew_full_sync",
            "end_full_sync",
            "fence_state",
            "capture_queued",
            "retire_captured",
        )
        if not all(callable(getattr(store, method, None)) for method in methods):
            raise NodeAPIError(-2, "Full snapshot coordination requires SnapshotUserSyncStoreProtocol")
        loop = asyncio.get_running_loop()
        started = loop.time()
        async with asyncio.timeout(lease_seconds):
            token = await store.begin_full_sync(self.node_id, self.worker_id, lease_seconds)
        if token is None:
            raise NodeAPIError(409, "A full user sync is already in progress for this node")
        held = FullSyncHold(asyncio.current_task(), started + lease_seconds)
        renewal = asyncio.create_task(self._renew_full_sync(token, lease_seconds, held))
        try:
            held.check()
            yield held
            held.check()
        except asyncio.CancelledError:
            if not held.lost:
                raise
            asyncio.current_task().uncancel()
            raise NodeAPIError(409, "Full user sync aborted after losing its lease") from None
        finally:
            renewal.cancel()
            with suppress(asyncio.CancelledError):
                await renewal
            with suppress(Exception):
                async with asyncio.timeout(self._internal_timeout):
                    await store.end_full_sync(self.node_id, token)
            await self.wake_sync_worker()

    async def capture_queued_work(self) -> dict[str, int]:
        """Under a full-sync fence, wait for deliveries and capture pending revisions."""
        if not await self.full_sync_in_progress():
            raise NodeAPIError(409, "Capture queued work inside full_sync_fence()")
        async with asyncio.timeout(self._sync_lease_seconds + 5):
            while True:
                captured, active = await self._user_sync_store.capture_queued(self.node_id)
                if not active:
                    return captured
                await asyncio.sleep(min(self._sync_poll_interval, 0.2))

    async def retire_queued_work(self, captured: dict[str, int]) -> None:
        """Retire only captured revisions after a successful authoritative snapshot."""
        await self._user_sync_store.retire_captured(self.node_id, captured)

    async def sync_users_from_source(
        self, load_users: Callable[[], Awaitable[list[User]]], timeout: int | None = None
    ) -> None:
        """Read a fresh full snapshot after quiescing deltas, then retire covered work."""
        async with self.full_sync_fence() as held:
            captured = await self.capture_queued_work()
            users = await load_users()
            held.check()
            await self.sync_users(users, flush_pending=False, timeout=timeout)
            held.check()
            await self.retire_queued_work(captured)

    async def _make_json_request(
        self,
        method: str,
        endpoint: str,
        timeout: int | None = None,
        json: dict | None = None,
    ) -> BufferedResponse:
        """Make an HTTP request to the node's REST API."""
        if timeout is None:
            timeout = self._default_timeout

        try:
            async with self._json_client.request(
                method=method, url=endpoint, json=json, timeout=make_timeout(timeout)
            ) as raw_response:
                response = await buffer_response(raw_response)
            response.raise_for_status()
            return response

        except BufferedStatusError as e:
            detail = ""
            try:
                data = e.response.json()
                if isinstance(data, dict):
                    detail = data.get("detail", "")
                else:
                    detail = str(data)
            except (JSONDecodeError, ValueError):
                detail = e.response.text

            raise NodeAPIError(code=e.response.status_code, detail=detail) from e

        except (TimeoutError, aiohttp.ClientError) as e:
            raise NodeAPIError(code=-5, detail=f"Request error: {e!s}") from e

    async def check_connectivity(self) -> bool:
        """Check if the node service is reachable via its REST API."""
        try:
            response = await self._make_json_request(method="GET", endpoint="/", timeout=5)
            return response.status_code == 200
        except NodeAPIError as e:
            self.logger.error(f"[{self.name}] Connectivity check failed: {e!s}")
            return False

    async def _run_coordinated_update(
        self,
        operation: LifecycleOperation,
        endpoint: str,
        json: dict | None = None,
    ) -> BufferedResponse:
        if not (await self.check_connectivity()):
            raise NodeAPIError(code=503, detail="Node service is not reachable")

        lease = await self._acquire_lifecycle_lease(operation)
        try:
            return await self._make_json_request(method="POST", endpoint=endpoint, json=json)
        finally:
            await self._release_lifecycle_lease(lease)

    async def update_node(self) -> BufferedResponse:
        """Trigger a node update via the REST API."""
        return await self._run_coordinated_update(LifecycleOperation.UPDATE_NODE, "/node/update")

    async def update_core(self, json: dict) -> BufferedResponse:
        """Trigger a node core update via the REST API."""
        return await self._run_coordinated_update(LifecycleOperation.UPDATE_CORE, "/node/core_update", json)

    async def update_geofiles(self, json: dict) -> BufferedResponse:
        """Trigger a node geofiles update via the REST API."""
        return await self._run_coordinated_update(LifecycleOperation.UPDATE_GEOFILES, "/node/geofiles", json)

    async def hard_reset(self) -> BufferedResponse:
        """Trigger a hard reset of the remote node service via the REST API.

        This posts to ``/node/hard_reset`` on the management API, which
        instructs ``node-serviced`` to run ``systemctl restart <service>``.
        The underlying ``pg-node`` process is restarted, so the node will be
        unreachable until it comes back up.

        After the call succeeds the local health state is set to NOT_CONNECTED
        so any subsequent operation (e.g. ``start()``) knows it must reconnect.

        Raises:
            NodeAPIError: code 503 if the management API is unreachable.
            NodeAPIError: code 409 if another lifecycle operation is in-flight.
            NodeAPIError: code 500 if systemctl reports a failure.
        """
        response = await self._run_coordinated_update(LifecycleOperation.HARD_RESET, "/node/hard_reset")
        # The remote service has been restarted; clear local state.
        await self.disconnect()
        return response
