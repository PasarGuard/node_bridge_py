import asyncio
import inspect
import logging
import math
import ssl
import sys
import traceback
from enum import IntEnum
from json import JSONDecodeError
from typing import Optional, cast
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
    LifecycleLeaseLostError,
    LifecycleOperation,
    LifecycleStatus,
    NodeLifecycleCoordinatorProtocol,
    NodeLifecycleState,
    RevocationAwareUserSyncStoreProtocol,
    StartupUserSyncLease,
    UserRevocationResult,
    UserSyncLease,
    UserSyncLeaseLostError,
    UserSyncStoreProtocol,
    get_default_lifecycle_coordinator,
    get_default_user_sync_store,
)

# Default timeout configuration (module-level constants)
DEFAULT_API_TIMEOUT = 10  # Default timeout for public API methods
DEFAULT_INTERNAL_TIMEOUT = 15  # Default timeout for internal gRPC/HTTP operations
CLAIM_RECOVERY_TIMEOUT = 1.0
SYNC_WORKER_CLEANUP_TIMEOUT = CLAIM_RECOVERY_TIMEOUT + 1.0
MIN_CLAIM_RECHECK_DELAY = 0.01
INITIAL_CLAIM_RETRY_DELAY = 1.0
MAX_CLAIM_RETRY_DELAY = 30.0
STALE_USER_SYNC_RETRY_LIMIT = 1


def _sanitize_log_text(value: object, limit: int = 2048) -> str:
    text = str(value)
    sanitized_parts = []
    for character in text:
        codepoint = ord(character)
        if codepoint in (0x2028, 0x2029):
            sanitized_parts.append(f"\\u{codepoint:04x}")
        elif codepoint < 32 or codepoint in range(127, 160):
            sanitized_parts.append(f"\\x{codepoint:02x}")
        else:
            sanitized_parts.append(character)
    sanitized = "".join(sanitized_parts)
    if len(sanitized) > limit:
        return f"{sanitized[:limit]}...[truncated]"
    return sanitized


class _SanitizingLoggerAdapter(logging.LoggerAdapter):
    def log(self, level, msg, *args, **kwargs):
        if not self.isEnabledFor(level):
            return

        if args:
            try:
                record = logging.LogRecord("", level, "", 0, msg, args, None)
                msg = record.getMessage()
                args = ()
            except Exception:  # noqa: BLE001,S110 - preserve logging's formatting-error behavior
                # Preserve logging's normal formatting-error behavior while
                # still sanitizing the format string itself.
                pass

        msg, kwargs = self.process(msg, kwargs)
        self.logger.log(level, msg, *args, **kwargs)

    def process(self, msg, kwargs):
        sanitized_message = _sanitize_log_text(msg)
        exc_info = kwargs.get("exc_info")
        if exc_info:
            try:
                if isinstance(exc_info, BaseException):
                    exc_info = (type(exc_info), exc_info, exc_info.__traceback__)
                elif exc_info is True or not isinstance(exc_info, tuple):
                    exc_info = sys.exc_info()
                formatted_exception = "".join(traceback.format_exception(*exc_info))
                sanitized_message = f"{sanitized_message} | Traceback: {_sanitize_log_text(formatted_exception)}"
                kwargs["exc_info"] = None
            except Exception:  # noqa: BLE001 - logging must not replace the operational exception
                kwargs["exc_info"] = None
        return sanitized_message, kwargs


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


class Controller:
    _REVOCATION_STORE_METHODS = (
        "begin_user_revocation",
        "abort_user_revocation",
        "finalize_user_revocation",
        "acquire_user_sync_lease",
        "acquire_startup_user_sync_lease",
        "retain_user_sync_lease_keys",
        "heartbeat_user_sync_lease",
        "release_user_sync_lease",
    )

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
    ):
        self.name = name
        self.node_id = node_id or service_url
        self.worker_id = worker_id or f"{self.node_id}:{id(self)}"
        if extra is None:
            extra = {}
        if logger is None:
            # Libraries should not install output handlers implicitly. Applications
            # that want bridge logs can configure this package logger or pass one.
            logger = logging.getLogger("PasarGuardNodeBridge")
        self.logger = _SanitizingLoggerAdapter(logger, {})

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
        self._user_sync_epoch_supported = False
        self._user_sync_epoch_capability_probed = False
        self._user_sync_epoch_handshake_lock = asyncio.Lock()
        self._user_sync_connection_generation = 0
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
            if self._user_sync_failure_count >= self._hard_reset_threshold:
                if not self._hard_reset_event.is_set():
                    self._hard_reset_event.set()
                    self.logger.critical(
                        f"[{self.name}] HARD RESET REQUIRED: User sync failed "
                        f"{self._user_sync_failure_count} times in a row"
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

    def _revocation_store(self, *, required: bool = False) -> RevocationAwareUserSyncStoreProtocol | None:
        store = getattr(self, "_user_sync_store", None)
        if store is not None and all(
            callable(getattr(store, method, None)) for method in self._REVOCATION_STORE_METHODS
        ):
            return cast(RevocationAwareUserSyncStoreProtocol, store)
        if required:
            raise NodeAPIError(501, "Configured user sync store does not support coordinated user revocation")
        return None

    @staticmethod
    def _normalize_user_keys(user_keys: list[str]) -> list[str]:
        unique_keys = list(dict.fromkeys(user_keys))
        if any(not isinstance(user_key, str) or not user_key for user_key in unique_keys):
            raise ValueError("user keys must be non-empty strings")
        return unique_keys

    async def _acquire_user_sync_lease(
        self,
        user_keys: list[str],
        expected_generations: dict[str, int] | None = None,
        revocation_id: str | None = None,
    ) -> UserSyncLease | None:
        store = self._revocation_store(required=revocation_id is not None)
        if store is None:
            return None
        return await store.acquire_user_sync_lease(
            self.node_id,
            self.worker_id,
            self._normalize_user_keys(user_keys),
            max(self._sync_lease_seconds, 0.1),
            expected_generations,
            revocation_id,
        )

    async def _heartbeat_user_sync_lease(self, lease: UserSyncLease) -> None:
        store = self._revocation_store(required=True)
        interval = max(lease.lease_seconds / 3, 0.01)
        try:
            while True:
                await asyncio.sleep(interval)
                if not await store.heartbeat_user_sync_lease(lease):
                    raise RuntimeError("User sync execution lease was lost")
        except asyncio.CancelledError:
            pass

    @staticmethod
    async def _await_cleanup_despite_cancellation(awaitable) -> asyncio.CancelledError | None:
        """Finish distributed cleanup before propagating caller cancellation.

        The cleanup runs in its own task and ``asyncio.wait`` observes it
        without propagating caller cancellation into that task. Any cleanup
        failure remains authoritative and is raised by ``task.result``.
        """
        cleanup = asyncio.ensure_future(awaitable)
        caller_cancellation: asyncio.CancelledError | None = None
        while not cleanup.done():
            try:
                await asyncio.wait((cleanup,))
            except asyncio.CancelledError as exc:
                current = asyncio.current_task()
                if current is None or not current.cancelling():
                    if cleanup.done():
                        break
                    raise
                caller_cancellation = exc
        cleanup.result()
        return caller_cancellation

    async def _release_user_sync_lease(
        self,
        lease: UserSyncLease | None,
        heartbeat: asyncio.Task | None = None,
    ) -> None:
        heartbeat_error: Exception | None = None
        caller_cancellation: asyncio.CancelledError | None = None
        if heartbeat is not None:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError as exc:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    caller_cancellation = exc
            except Exception as exc:  # noqa: BLE001 - report a failed distributed heartbeat after cleanup
                heartbeat_error = exc
        if heartbeat_error is not None:
            self.logger.error(
                f"[{self.name}] User sync execution lease heartbeat failed | "
                f"Error: {type(heartbeat_error).__name__} - {heartbeat_error!s}"
            )
            raise UserSyncLeaseLostError("user-sync lease ownership was lost before completion") from heartbeat_error
        if lease is not None:
            store = self._revocation_store(required=True)
            cleanup_cancellation = await self._await_cleanup_despite_cancellation(store.release_user_sync_lease(lease))
            if caller_cancellation is None:
                caller_cancellation = cleanup_cancellation
        if caller_cancellation is not None:
            raise caller_cancellation

    async def _abandon_user_sync_lease(
        self,
        lease: UserSyncLease | None,
        heartbeat: asyncio.Task | None = None,
    ) -> None:
        """Stop renewing a lease whose remote write outcome is unknown.

        The store record is deliberately retained. Once it expires, coordinated
        revocation must fail closed until an operator explicitly reconciles and
        releases that exact lease.
        """
        caller_cancellation: asyncio.CancelledError | None = None
        if heartbeat is not None:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError as exc:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    caller_cancellation = exc
            except Exception as exc:  # noqa: BLE001 - the retained lease is already fail-closed
                self.logger.error(
                    f"[{self.name}] User sync execution lease heartbeat failed | Error: {type(exc).__name__} - {exc!s}"
                )
        if lease is not None:
            self.logger.error(
                f"[{self.name}] User sync outcome is unknown; retaining execution lease {lease.token!r} "
                "for explicit reconciliation"
            )
        if caller_cancellation is not None:
            raise caller_cancellation

    async def _retain_unknown_user_sync_lease_keys(
        self,
        lease: UserSyncLease | None,
        heartbeat: asyncio.Task | None,
        unknown_user_keys: list[str],
    ) -> None:
        """Release known outcomes while retaining a poison lease for unknown keys."""
        if lease is None:
            return
        unknown_keys = self._normalize_user_keys(unknown_user_keys)
        if set(unknown_keys) == set(lease.user_keys):
            await self._abandon_user_sync_lease(lease, heartbeat)
            return

        # Stop the task that holds the original lease value before replacing
        # that value in the store. Otherwise it can race the narrowing update,
        # observe a different lease object, and report a false ownership loss.
        await self._abandon_user_sync_lease(None, heartbeat)
        try:
            store = self._revocation_store(required=True)
            narrowed = await store.retain_user_sync_lease_keys(lease, unknown_keys)
        except BaseException:
            await self._abandon_user_sync_lease(lease)
            raise
        await self._abandon_user_sync_lease(narrowed)

    async def _acquire_direct_user_sync_lease(
        self,
        users: list[User],
        revocation_id: str | None = None,
    ) -> tuple[UserSyncLease | None, asyncio.Task | None]:
        await self._probe_user_sync_epoch_capability()
        if revocation_id is not None:
            await self._ensure_user_sync_epoch_support()
        user_keys = self._normalize_user_keys([user.email for user in users])
        lease = await self._acquire_user_sync_lease(user_keys, revocation_id=revocation_id)
        if lease is None:
            return None, None
        denied_keys = set(user_keys).difference(lease.user_keys)
        if denied_keys:
            await self._release_user_sync_lease(lease)
            raise NodeAPIError(409, f"User sync is fenced for {len(denied_keys)} user(s)")
        heartbeat = asyncio.create_task(self._heartbeat_user_sync_lease(lease)) if lease.token else None
        return lease, heartbeat

    async def _acquire_snapshot_user_sync_lease(
        self,
        users: list[User],
    ) -> tuple[list[User], UserSyncLease | None, asyncio.Task | None]:
        """Acquire a node-wide replacement permit and omit permanently fenced users."""
        await self._probe_user_sync_epoch_capability()
        store = self._revocation_store()
        if store is None:
            return users, None, None
        startup: StartupUserSyncLease = await store.acquire_startup_user_sync_lease(
            self.node_id,
            self.worker_id,
            self._normalize_user_keys([user.email for user in users]),
            max(self._sync_lease_seconds, 0.1),
        )
        allowed_keys = set(startup.included_user_keys)
        filtered_users = [user for user in users if user.email in allowed_keys]
        lease = startup.lease
        heartbeat = asyncio.create_task(self._heartbeat_user_sync_lease(lease)) if lease.token else None
        return filtered_users, lease, heartbeat

    async def _acquire_reconciliation_user_sync_lease(
        self,
        users: list[User],
    ) -> tuple[list[User], UserSyncLease, asyncio.Task]:
        """Acquire the explicit full-snapshot recovery permit."""
        await self._ensure_user_sync_epoch_support()
        store = self._revocation_store(required=True)
        acquire_reconciliation = getattr(store, "acquire_user_sync_reconciliation_lease", None)
        if not callable(acquire_reconciliation):
            raise NodeAPIError(501, "Configured user sync store does not support authoritative reconciliation")
        recovery: StartupUserSyncLease = await acquire_reconciliation(
            self.node_id,
            self.worker_id,
            self._normalize_user_keys([user.email for user in users]),
            max(self._sync_lease_seconds, 0.1),
        )
        allowed_keys = set(recovery.included_user_keys)
        filtered_users = [user for user in users if user.email in allowed_keys]
        lease = recovery.lease
        heartbeat = asyncio.create_task(self._heartbeat_user_sync_lease(lease))
        return filtered_users, lease, heartbeat

    async def _assert_user_sync_lease_owned(self, lease: UserSyncLease | None) -> None:
        """Revalidate a permit immediately before a remote side effect."""
        if lease is None or not lease.token:
            return
        store = self._revocation_store(required=True)
        if not await store.heartbeat_user_sync_lease(lease):
            raise UserSyncLeaseLostError("user-sync execution lease was lost before transport")

    async def _observe_user_sync_epoch_capability(
        self,
        info: object,
        expected_generation: int | None = None,
    ) -> None:
        lock = getattr(self, "_user_sync_epoch_handshake_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._user_sync_epoch_handshake_lock = lock
        async with lock:
            if expected_generation is not None and expected_generation != getattr(
                self, "_user_sync_connection_generation", 0
            ):
                return
            supported = bool(getattr(info, "user_sync_epoch_supported", False))
            if supported:
                store = self._revocation_store()
                if store is not None:
                    advance_epoch = getattr(store, "advance_user_sync_epoch", None)
                    if not callable(advance_epoch):
                        self._user_sync_epoch_supported = False
                        self._user_sync_epoch_capability_probed = False
                        raise NodeAPIError(501, "Configured user sync store does not support epoch handshakes")
                    await advance_epoch(self.node_id, int(getattr(info, "user_sync_epoch", 0)))
            self._user_sync_epoch_supported = supported
            self._user_sync_epoch_capability_probed = True

    def _require_user_sync_epoch_support(self) -> None:
        if not getattr(self, "_user_sync_epoch_supported", False):
            raise NodeAPIError(426, "Node does not advertise monotonic user-sync epoch fencing")

    async def _ensure_user_sync_epoch_support(self) -> None:
        await self._probe_user_sync_epoch_capability()
        self._require_user_sync_epoch_support()

    async def _probe_user_sync_epoch_capability(self) -> None:
        if getattr(self, "_user_sync_epoch_capability_probed", False):
            return
        info_method = getattr(self, "info", None)
        if not callable(info_method):
            self._user_sync_epoch_capability_probed = True
            return
        capability_generation = getattr(self, "_user_sync_connection_generation", 0)
        info = await info_method()
        if info is not None and not getattr(self, "_user_sync_epoch_capability_probed", False):
            await self._observe_user_sync_epoch_capability(info, capability_generation)

    def _user_sync_epoch_for_transport(self, lease: UserSyncLease | None) -> int:
        if lease is None or not getattr(self, "_user_sync_epoch_supported", False):
            return 0
        return lease.epoch

    @staticmethod
    def _is_stale_user_sync_rejection(error: BaseException) -> bool:
        """Return whether the node rejected an epoch before applying anything."""
        if isinstance(error, NodeAPIError):
            return error.code == 412
        status = getattr(error, "status", None)
        return getattr(status, "name", None) == "FAILED_PRECONDITION"

    @staticmethod
    def _accepts_user_sync_epoch(callback: object) -> bool:
        """Preserve legacy transport hooks without masking callback errors."""
        try:
            parameters = inspect.signature(callback).parameters
        except (TypeError, ValueError):
            return True
        return "user_sync_epoch" in parameters or any(
            parameter.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
            for parameter in parameters.values()
        )

    async def begin_user_revocation(self, user_keys: list[str], revocation_id: str) -> UserRevocationResult:
        """Fence keys and wait until older queued/in-flight updates cannot run."""
        if not revocation_id:
            raise ValueError("revocation_id must not be empty")
        await self._ensure_user_sync_epoch_support()
        store = self._revocation_store(required=True)
        return await store.begin_user_revocation(self.node_id, self._normalize_user_keys(user_keys), revocation_id)

    async def abort_user_revocation(self, user_keys: list[str], revocation_id: str) -> None:
        """Release only this provisional revocation after authoritative restore."""
        if not revocation_id:
            raise ValueError("revocation_id must not be empty")
        store = self._revocation_store(required=True)
        await store.abort_user_revocation(self.node_id, self._normalize_user_keys(user_keys), revocation_id)

    async def finalize_user_revocation(self, user_keys: list[str], revocation_id: str) -> None:
        """Convert this operation's provisional fences to permanent tombstones."""
        if not revocation_id:
            raise ValueError("revocation_id must not be empty")
        store = self._revocation_store(required=True)
        await store.finalize_user_revocation(self.node_id, self._normalize_user_keys(user_keys), revocation_id)

    async def update_user(self, user: User):
        """Queue a user for sync. Automatically deduplicates by email."""
        lease = await self._acquire_user_sync_lease([user.email])
        try:
            if lease is not None and user.email not in lease.user_keys:
                return
            await self._user_sync_store.enqueue_users(self.node_id, [user])
        finally:
            await self._release_user_sync_lease(lease)
        self._work_available.set()

        # Ensure worker is running to process the update
        await self._ensure_sync_worker_running()

    async def update_users(self, users: list[User]):
        """Queue multiple users for sync. Automatically deduplicates by email."""
        if not users:
            return

        lease = await self._acquire_user_sync_lease([user.email for user in users])
        try:
            if lease is not None:
                allowed_keys = set(lease.user_keys)
                users = [user for user in users if user.email in allowed_keys]
                if not users:
                    return
            await self._user_sync_store.enqueue_users(self.node_id, users)
        finally:
            await self._release_user_sync_lease(lease)
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
        self._work_available.clear()

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

    @property
    def extra(self) -> dict:
        """Backward-compatible access to node metadata.

        New asynchronous code should prefer :meth:`get_extra` when it needs
        metadata coordinated with controller state updates.
        """

        return self._extra

    @extra.setter
    def extra(self, value: dict) -> None:
        self._extra = value

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
                if not await self._lifecycle_coordinator.heartbeat(lease):
                    raise LifecycleLeaseLostError(f"Lifecycle lease ownership was lost for node {self.node_id}")
        except asyncio.CancelledError:
            pass

    async def _acquire_lifecycle_lease(self, operation: LifecycleOperation) -> LifecycleLease:
        try:
            lease = await self._lifecycle_coordinator.try_acquire(
                self.node_id, self.worker_id, operation, self._lifecycle_lease_seconds
            )
        except Exception as exc:
            raise NodeAPIError(503, "Lifecycle coordinator acquire failed") from exc
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
        heartbeat = getattr(self, "_lifecycle_heartbeat_tasks", {}).pop(lease.token, None)
        heartbeat_error: BaseException | None = None
        caller_cancellation: asyncio.CancelledError | None = None
        if heartbeat is not None:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError as exc:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    caller_cancellation = exc
            except BaseException as exc:  # preserve the unknown remote outcome
                heartbeat_error = exc

        try:
            if observed is None:
                cleanup_cancellation = await self._await_cleanup_despite_cancellation(
                    self._lifecycle_coordinator.release(lease)
                )
            else:
                state = NodeLifecycleState(
                    desired=desired or observed,
                    observed=observed,
                    epoch=lease.epoch,
                    operation=lease.operation,
                    owner=lease.worker_id,
                    node_version=node_version,
                    core_version=core_version,
                )
                cleanup_cancellation = await self._await_cleanup_despite_cancellation(
                    self._lifecycle_coordinator.release(lease, state)
                )
        except Exception as exc:
            raise NodeAPIError(503, "Lifecycle coordinator release failed") from exc

        if heartbeat_error is not None:
            # The transport result is already known and the coordinator has
            # recorded it. A late heartbeat failure must not turn that known
            # success into a retained poison lease.
            self.logger.error(
                "[%s] Lifecycle heartbeat failed after the remote outcome was recorded | Error: %s - %s",
                self.name,
                type(heartbeat_error).__name__,
                heartbeat_error,
            )

        if observed is None:
            if caller_cancellation is None:
                caller_cancellation = cleanup_cancellation
            if caller_cancellation is not None:
                raise caller_cancellation
            return
        if caller_cancellation is None:
            caller_cancellation = cleanup_cancellation
        if caller_cancellation is not None:
            raise caller_cancellation

    async def _stop_lifecycle_heartbeat(self, lease: LifecycleLease | None) -> None:
        """Stop renewal after a failed operation without masking its error."""
        if lease is None:
            return
        heartbeat = getattr(self, "_lifecycle_heartbeat_tasks", {}).pop(lease.token, None)
        if heartbeat is None:
            return
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
        except Exception:
            self.logger.exception("[%s] Lifecycle heartbeat failed during cleanup", self.name)

    async def get_lifecycle_state(self) -> NodeLifecycleState | None:
        try:
            return await self._lifecycle_coordinator.get_state(self.node_id)
        except Exception as exc:
            raise NodeAPIError(503, "Lifecycle coordinator state read failed") from exc

    async def reconcile_lifecycle(self, observed: LifecycleStatus) -> None:
        """Acknowledge an inspected remote state after an expired operation.

        Reconciliation is intentionally explicit: an expired lifecycle request
        is an unknown remote effect and must not be replaced by a new operation
        until a caller has probed the node and supplied the observed state.
        """
        try:
            reconciled = await self._lifecycle_coordinator.reconcile(self.node_id, observed)
        except Exception as exc:
            raise NodeAPIError(503, "Lifecycle coordinator reconciliation failed") from exc
        if not reconciled:
            raise NodeAPIError(409, f"Node lifecycle operation is still active for {self.node_id}")

    async def update_observed_lifecycle(self, observed: LifecycleStatus, expected_epoch: int | None = None) -> None:
        try:
            await self._lifecycle_coordinator.update_observed(self.node_id, observed, expected_epoch)
        except Exception as exc:
            raise NodeAPIError(503, "Lifecycle coordinator state update failed") from exc

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

        # Updates can be queued while disconnected, including by another
        # controller sharing the store. Wake a worker after every successful
        # reconnect; it will exit normally after the idle timeout if no work exists.
        self._work_available.set()
        await self._ensure_sync_worker_running()

    async def disconnect(self):
        # Set shutdown event (no lock needed)
        self._shutdown_event.set()

        handshake_lock = getattr(self, "_user_sync_epoch_handshake_lock", None)
        if handshake_lock is None:
            handshake_lock = asyncio.Lock()
            self._user_sync_epoch_handshake_lock = handshake_lock
        async with handshake_lock:
            self._user_sync_connection_generation = getattr(self, "_user_sync_connection_generation", 0) + 1
            self._user_sync_epoch_supported = False
            self._user_sync_epoch_capability_probed = False

        # Cleanup tasks
        async with self._task_lock:
            await self._cleanup_tasks()

        # Stop this controller's worker without deleting work from the shared
        # store. Pending updates are resumed by this or another controller.
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
        """Stop this controller's sync worker while preserving shared pending users."""
        # Cancel sync worker if running
        async with self._sync_worker_lock:
            task = self._sync_worker_task
            if task is not None and not task.done():
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=SYNC_WORKER_CLEANUP_TIMEOUT)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
                except Exception as e:  # noqa: BLE001 - cleanup must complete even if worker recovery fails
                    self.logger.warning(
                        f"[{self.name}] Sync worker cleanup observed an error | Error: {type(e).__name__} - {e!s}"
                    )
            if self._sync_worker_task is task:
                self._sync_worker_task = None

        self._work_available.clear()

    def is_shutting_down(self) -> bool:
        """Check if the node is shutting down"""
        return self._shutdown_event.is_set()

    async def _ensure_sync_worker_running(self):
        """Spawn sync worker if not already running."""
        async with self._sync_worker_lock:
            if self._sync_worker_task is None or self._sync_worker_task.done():
                task = asyncio.create_task(self._sync_worker())
                self._sync_worker_task = task
                task.add_done_callback(self._clear_finished_sync_worker)

    def _clear_finished_sync_worker(self, task: asyncio.Task) -> None:
        """Drop a completed worker reference without racing a replacement worker."""
        if self._sync_worker_task is task:
            self._sync_worker_task = None

    async def _retire_sync_worker_if_idle(self) -> bool:
        """Atomically retire the current worker unless a concurrent enqueue woke it."""
        current_task = asyncio.current_task()
        async with self._sync_worker_lock:
            # update_user(s) sets the event before taking this lock in
            # _ensure_sync_worker_running(). If that enqueue won the race, the
            # current worker must keep running and consume the queued update.
            if self._work_available.is_set():
                return False

            # Publish retirement before leaving the worker. An enqueue that
            # happens after this point will observe None and start a replacement.
            # Keep the identity check so an older worker can never clear a newer
            # worker that was installed while it was finishing.
            if self._sync_worker_task is current_task:
                self._sync_worker_task = None
            return True

    async def _claim_pending_users(self, limit: int = 2000) -> list[ClaimedUser]:
        """Claim pending users from the configured sync store."""
        # Clear before crossing the storage await boundary. An enqueue that
        # races with claim_users() will set the event afterwards and must not
        # be erased when an eventually-empty claim returns.
        self._work_available.clear()
        claimed = await self._user_sync_store.claim_users(
            self.node_id, self.worker_id, limit=limit, lease_seconds=self._sync_lease_seconds
        )
        if claimed:
            # Keep draining. The store may still contain more than one claim
            # batch, and one harmless empty claim restores the idle state.
            self._work_available.set()
        return claimed

    async def _next_claim_delay(self) -> tuple[bool, float | None]:
        """Return whether the store can report the next claimable-work deadline."""
        next_claim_delay = getattr(self._user_sync_store, "next_claim_delay", None)
        if next_claim_delay is None:
            return False, None
        return True, await next_claim_delay(self.node_id)

    async def _wait_for_claim_recheck(self, delay: float) -> None:
        """Sleep until a store lease may expire, while remaining locally wakeable."""
        # A distributed/custom store may transiently report a due deadline
        # while another worker wins the claim. Always yield for a real,
        # positive interval even when polling is explicitly disabled.
        wait_delay = max(delay, self._sync_poll_interval, MIN_CLAIM_RECHECK_DELAY)
        try:
            await asyncio.wait_for(self._work_available.wait(), timeout=wait_delay)
        except asyncio.TimeoutError:
            # Re-arm the normal worker loop after the bounded wait. A local
            # enqueue can also set the event and wake this wait early.
            self._work_available.set()

    async def _ack_claimed_users(self, claimed_users: list[ClaimedUser]):
        await self._user_sync_store.ack_users(self.node_id, [item.token for item in claimed_users])

    async def _requeue_claimed_users(self, claimed_users: list[ClaimedUser]):
        await self._user_sync_store.requeue_users(self.node_id, claimed_users)
        if claimed_users:
            self._work_available.set()

    async def _recover_claimed_users(self, claimed_users: list[ClaimedUser], context: str) -> bool:
        """Attempt bounded claim recovery; an unexpired store lease remains the fallback."""
        if not claimed_users:
            return True
        recovery_task = asyncio.create_task(self._requeue_claimed_users(claimed_users))

        def abandon_recovery(error: BaseException) -> bool:
            recovery_task.cancel()
            recovery_task.add_done_callback(lambda task: None if task.cancelled() else task.exception())
            self.logger.error(
                f"[{self.name}] Failed to recover {len(claimed_users)} claimed user(s) after {context} | "
                f"Error: {type(error).__name__}"
            )
            return False

        try:
            done, _ = await asyncio.wait((recovery_task,), timeout=CLAIM_RECOVERY_TIMEOUT)
            if not done:
                return abandon_recovery(TimeoutError())
            await recovery_task
            return True
        except asyncio.CancelledError as requeue_error:
            return abandon_recovery(requeue_error)
        except Exception as requeue_error:  # noqa: BLE001 - the lease is the fallback for any storage failure
            return abandon_recovery(requeue_error)

    async def _sync_worker(self):
        """Lazy worker that processes pending users and exits when idle."""
        self.logger.debug(f"[{self.name}] Sync worker started")
        retry_delay = 1.0
        max_retry_delay = 30.0
        claim_retry_delay = INITIAL_CLAIM_RETRY_DELAY
        supports_chunked, node_version = await self._supports_chunked_sync()
        legacy_lease_recheck_deadline: float | None = None
        if not supports_chunked:
            self.logger.debug(
                f"[{self.name}] Chunked sync disabled for node version '{node_version or 'unknown'}' (< v0.2.0)"
            )

        claimed_users: list[ClaimedUser] = []
        user_sync_lease: UserSyncLease | None = None
        user_sync_heartbeat: asyncio.Task | None = None
        user_sync_remote_started = False
        user_sync_remote_completed = False
        try:
            while not self.is_shutting_down():
                # Wait for work or timeout
                try:
                    await asyncio.wait_for(self._work_available.wait(), timeout=self._worker_idle_timeout)
                except asyncio.TimeoutError:
                    if await self._retire_sync_worker_if_idle():
                        self.logger.debug(f"[{self.name}] Sync worker idle, exiting")
                        break
                    # An enqueue set the wake event before acquiring the worker
                    # lock. Keep this worker rather than letting ensure() observe
                    # a task that is about to exit.
                    continue

                # Check health - don't sync if not connected or invalid
                health = await self.get_health()
                if health == Health.NOT_CONNECTED:
                    self.logger.debug(f"[{self.name}] Sync worker exiting - not connected")
                    break
                if health == Health.INVALID:
                    self.logger.debug(f"[{self.name}] Sync worker exiting - node invalid")
                    break

                # If BROKEN, wait and loop back without draining users
                if health == Health.BROKEN:
                    self.logger.warning(f"[{self.name}] Node is broken, waiting {retry_delay}s before retry")
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, max_retry_delay)
                    continue

                # Claim pending users atomically (only when healthy)
                try:
                    claimed_users = await self._claim_pending_users()
                    claim_retry_delay = INITIAL_CLAIM_RETRY_DELAY
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 - storage failures must not strand queued work
                    # _claim_pending_users clears the event before awaiting the
                    # store. Re-arm it so pending work remains discoverable even
                    # when an enqueue raced with this failed claim. Retrying in
                    # the existing worker also closes the completion/ensure race
                    # without ever creating a second worker.
                    self._work_available.set()
                    self.logger.warning(
                        f"[{self.name}] Failed to claim pending users, retrying in {claim_retry_delay}s | "
                        f"Error: {type(e).__name__} - {e!s}"
                    )
                    await asyncio.sleep(claim_retry_delay)
                    claim_retry_delay = min(claim_retry_delay * 2, MAX_CLAIM_RETRY_DELAY)
                    continue
                if not claimed_users:
                    lease_aware, next_claim_delay = await self._next_claim_delay()
                    if lease_aware:
                        legacy_lease_recheck_deadline = None
                        if next_claim_delay is not None:
                            await self._wait_for_claim_recheck(next_claim_delay)
                        # A lease-aware store reporting no tracked work is
                        # genuinely idle. Loop directly back to the original
                        # event/idle-timeout wait without adding poll latency.
                    else:
                        # Backward compatibility for custom stores created
                        # before next_claim_delay existed. Wait for at most one
                        # configured lease horizon, then restore normal idle exit.
                        loop = asyncio.get_running_loop()
                        if legacy_lease_recheck_deadline is None:
                            legacy_lease_recheck_deadline = loop.time() + max(self._sync_lease_seconds, 0.0)
                        remaining = legacy_lease_recheck_deadline - loop.time()
                        if remaining > 0:
                            await self._wait_for_claim_recheck(remaining)
                        else:
                            legacy_lease_recheck_deadline = None
                            await asyncio.sleep(self._sync_poll_interval)
                    continue
                legacy_lease_recheck_deadline = None
                expected_generations = {item.user.email: item.generation for item in claimed_users}
                user_sync_lease = await self._acquire_user_sync_lease(list(expected_generations), expected_generations)
                if user_sync_lease is not None:
                    allowed_keys = set(user_sync_lease.user_keys)
                    suppressed_claims = [item for item in claimed_users if item.user.email not in allowed_keys]
                    if suppressed_claims:
                        await self._ack_claimed_users(suppressed_claims)
                    claimed_users = [item for item in claimed_users if item.user.email in allowed_keys]
                    if user_sync_lease.token:
                        user_sync_heartbeat = asyncio.create_task(self._heartbeat_user_sync_lease(user_sync_lease))
                if not claimed_users:
                    await self._release_user_sync_lease(user_sync_lease, user_sync_heartbeat)
                    user_sync_lease = None
                    user_sync_heartbeat = None
                    continue
                users = [item.user for item in claimed_users]
                user_sync_remote_started = False
                user_sync_remote_completed = False

                # Prefer chunked sync for large batches to reduce per-request overhead
                chunked_transport = getattr(self, "_sync_users_chunked_transport", None)
                use_chunked = supports_chunked and len(users) >= 1000 and callable(chunked_transport)
                if supports_chunked and len(users) >= 1000 and not use_chunked:
                    self.logger.debug(
                        f"[{self.name}] Protected chunked transport is unavailable; using compatible batch sync"
                    )
                if use_chunked:
                    # Aim for ~10 chunks, cap size to 2000 to stay under server limits
                    chunk_size = min(2000, max(1, math.ceil(len(users) / 10)))
                    try:
                        async with self._node_lock:
                            await self._assert_user_sync_lease_owned(user_sync_lease)
                            user_sync_remote_started = True
                            if self._accepts_user_sync_epoch(chunked_transport):
                                await chunked_transport(
                                    users,
                                    chunk_size,
                                    self._internal_timeout,
                                    self._user_sync_epoch_for_transport(user_sync_lease),
                                )
                            else:
                                await chunked_transport(users, chunk_size, self._internal_timeout)
                        failed_users = []
                        user_sync_remote_completed = True
                    except Exception as e:  # noqa: BLE001 - preserve the worker's retry contract
                        if self._is_stale_user_sync_rejection(e):
                            await self._release_user_sync_lease(user_sync_lease, user_sync_heartbeat)
                            user_sync_lease = None
                            user_sync_heartbeat = None
                            user_sync_remote_completed = True
                            await self._requeue_claimed_users(claimed_users)
                            claimed_users = []
                            await asyncio.sleep(retry_delay)
                            retry_delay = min(retry_delay * 2, max_retry_delay)
                            continue
                        error_type = type(e).__name__
                        self.logger.warning(
                            f"[{self.name}] Chunked sync failed for {len(users)} user(s) | Error: {error_type} - {e!s}"
                        )
                        failed_users = users
                    if failed_users:
                        self.logger.warning(
                            f"[{self.name}] {len(failed_users)}/{len(users)} users failed to chunk-sync "
                            f"(chunk_size={chunk_size})"
                        )
                        failed_emails = {user.email for user in failed_users}
                        failed_claims = [item for item in claimed_users if item.user.email in failed_emails]
                        await self._retain_unknown_user_sync_lease_keys(
                            user_sync_lease, user_sync_heartbeat, list(failed_emails)
                        )
                        user_sync_lease = None
                        user_sync_heartbeat = None
                        await self._ack_claimed_users(
                            [item for item in claimed_users if item.user.email not in failed_emails]
                        )
                        claimed_users = failed_claims
                        await self._requeue_claimed_users(claimed_users)
                        claimed_users = []
                        await self._increment_user_sync_failure()
                        await asyncio.sleep(retry_delay)
                        retry_delay = min(retry_delay * 2, max_retry_delay)
                    else:
                        self.logger.debug(
                            f"[{self.name}] Chunk-synced {len(users)} user(s) with chunk_size={chunk_size}"
                        )
                        await self._ack_claimed_users(claimed_users)
                        claimed_users = []
                        await self._reset_user_sync_failure_count()
                        retry_delay = 1.0
                else:
                    # Batch sync users individually
                    try:
                        await self._assert_user_sync_lease_owned(user_sync_lease)
                        user_sync_remote_started = True
                        if self._accepts_user_sync_epoch(self._sync_batch_users):
                            failed_users = await self._sync_batch_users(
                                users,
                                self._user_sync_epoch_for_transport(user_sync_lease),
                            )
                        else:
                            failed_users = await self._sync_batch_users(users)
                        user_sync_remote_completed = not failed_users
                        if failed_users:
                            self.logger.warning(f"[{self.name}] {len(failed_users)}/{len(users)} users failed to sync")
                            failed_emails = {user.email for user in failed_users}
                            failed_claims = [item for item in claimed_users if item.user.email in failed_emails]
                            await self._retain_unknown_user_sync_lease_keys(
                                user_sync_lease, user_sync_heartbeat, list(failed_emails)
                            )
                            user_sync_lease = None
                            user_sync_heartbeat = None
                            await self._ack_claimed_users(
                                [item for item in claimed_users if item.user.email not in failed_emails]
                            )
                            claimed_users = failed_claims
                            await self._requeue_claimed_users(claimed_users)
                            claimed_users = []
                            await self._increment_user_sync_failure()
                            # Exponential backoff on partial failure
                            await asyncio.sleep(retry_delay)
                            retry_delay = min(retry_delay * 2, max_retry_delay)
                        else:
                            self.logger.debug(f"[{self.name}] Synced {len(users)} user(s)")
                            await self._ack_claimed_users(claimed_users)
                            claimed_users = []
                            await self._reset_user_sync_failure_count()
                            retry_delay = 1.0  # Reset retry delay on success

                    except Exception as e:
                        stale_epoch = self._is_stale_user_sync_rejection(e)
                        error_type = type(e).__name__
                        self.logger.warning(
                            f"[{self.name}] Batch sync failed for {len(users)} user(s), requeuing | "
                            f"Error: {error_type} - {e!s}"
                        )
                        await self._increment_user_sync_failure()
                        if stale_epoch:
                            # Release the known-unapplied write before any
                            # fallible queue cleanup can replace this error.
                            await self._release_user_sync_lease(user_sync_lease, user_sync_heartbeat)
                            user_sync_lease = None
                            user_sync_heartbeat = None
                            user_sync_remote_completed = True
                        await self._requeue_claimed_users(claimed_users)
                        claimed_users = []
                        if not stale_epoch:
                            if user_sync_remote_started and not user_sync_remote_completed:
                                await self._abandon_user_sync_lease(user_sync_lease, user_sync_heartbeat)
                            else:
                                await self._release_user_sync_lease(user_sync_lease, user_sync_heartbeat)
                        user_sync_lease = None
                        user_sync_heartbeat = None
                        # Exponential backoff on failure
                        await asyncio.sleep(retry_delay)
                        retry_delay = min(retry_delay * 2, max_retry_delay)

                if user_sync_lease is not None:
                    await self._release_user_sync_lease(user_sync_lease, user_sync_heartbeat)
                    user_sync_lease = None
                    user_sync_heartbeat = None

        except asyncio.CancelledError:
            await self._recover_claimed_users(claimed_users, "worker cancellation")
            claimed_users = []
            if user_sync_remote_started and not user_sync_remote_completed:
                await self._abandon_user_sync_lease(user_sync_lease, user_sync_heartbeat)
            else:
                await self._release_user_sync_lease(user_sync_lease, user_sync_heartbeat)
            user_sync_lease = None
            user_sync_heartbeat = None
            self.logger.debug(f"[{self.name}] Sync worker cancelled")
        except Exception as e:
            error_type = type(e).__name__
            self.logger.exception(f"[{self.name}] Unexpected error in sync worker | Error: {error_type}")
            await self._recover_claimed_users(claimed_users, "worker error")
            claimed_users = []
            if user_sync_remote_started and not user_sync_remote_completed:
                await self._abandon_user_sync_lease(user_sync_lease, user_sync_heartbeat)
            else:
                await self._release_user_sync_lease(user_sync_lease, user_sync_heartbeat)
            user_sync_lease = None
            user_sync_heartbeat = None
        finally:
            if user_sync_lease is not None:
                if user_sync_remote_started and not user_sync_remote_completed:
                    await self._abandon_user_sync_lease(user_sync_lease, user_sync_heartbeat)
                else:
                    await self._release_user_sync_lease(user_sync_lease, user_sync_heartbeat)
            self.logger.debug(f"[{self.name}] Sync worker finished")

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
            response = await self._make_json_request(method="POST", endpoint=endpoint, json=json)
        except BaseException:
            # A timeout/cancellation after dispatch has an unknown remote
            # outcome. Keep the shared lease as poison until an explicit
            # lifecycle reconciliation observes the actual node state.
            await self._stop_lifecycle_heartbeat(lease)
            raise
        await self._release_lifecycle_lease(lease)
        return response

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
