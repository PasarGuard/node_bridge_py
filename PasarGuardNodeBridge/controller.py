import asyncio
import logging
import ssl
from enum import IntEnum
from json import JSONDecodeError
from typing import Optional
from uuid import UUID

import httpx

from PasarGuardNodeBridge.common.service_pb2 import User

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
    ):
        self.name = name
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

            self.ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
            self.ctx.set_alpn_protocols(["h2"])
            self.ctx.load_verify_locations(cadata=server_ca)
            self.ctx.check_hostname = True

        except ssl.SSLError as e:
            raise NodeAPIError(-1, f"SSL initialization failed: {str(e)}")

        except (ValueError, TypeError) as e:
            raise NodeAPIError(-2, f"Invalid API key format: {str(e)}")

        self._health = Health.NOT_CONNECTED
        self._tasks: list[asyncio.Task] = []
        self._node_version = ""
        self._core_version = ""
        self._extra = extra

        # Lazy worker sync mechanism
        self._pending_users: dict[str, User] = {}  # email -> User (auto-dedup)
        self._sync_worker_task: asyncio.Task | None = None
        self._work_available = asyncio.Event()
        self._worker_idle_timeout = 5.0  # seconds before worker exits

        # Hard reset mechanism for critical failures
        self._hard_reset_event = asyncio.Event()
        self._user_sync_failure_count = 0
        self._hard_reset_threshold = 5
        self._failure_count_lock = asyncio.Lock()  # Only for incrementing counters

        # Separate locks for different resources to reduce contention
        self._health_lock = asyncio.Lock()
        self._pending_users_lock = asyncio.Lock()
        self._sync_worker_lock = asyncio.Lock()
        self._version_lock = asyncio.Lock()
        self._task_lock = asyncio.Lock()

        self._shutdown_event = asyncio.Event()

        httpx_timeout = httpx.Timeout(
            default_timeout, connect=default_timeout, read=default_timeout, write=default_timeout
        )
        self._json_client = httpx.AsyncClient(
            http2=True,
            verify=self.ctx,
            headers={"Content-Type": "application/json", "x-api-key": api_key},
            base_url=service_url,
            timeout=httpx_timeout,
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
        async with self._pending_users_lock:
            self._pending_users[user.email] = user  # Latest version wins
            self._work_available.set()

        # Ensure worker is running to process the update
        await self._ensure_sync_worker_running()

    async def update_users(self, users: list[User]):
        """Queue multiple users for sync. Automatically deduplicates by email."""
        if not users:
            return

        async with self._pending_users_lock:
            for user in users:
                self._pending_users[user.email] = user  # Latest version wins
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
                f"Error: {error_type} - {str(e)}"
            )
            return None, None  # Keep current delays

    async def flush_pending_users(self):
        """Clear all pending users without syncing them."""
        async with self._pending_users_lock:
            self._pending_users.clear()
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
        async with self._health_lock:
            async with self._version_lock:
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

    async def disconnect(self):
        # Set shutdown event (no lock needed)
        self._shutdown_event.set()

        # Cleanup tasks
        async with self._task_lock:
            await self._cleanup_tasks()

        # Cleanup sync worker and pending users
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
                            f"[{self.name}] Task {i} raised exception during cleanup | "
                            f"Error: {error_type} - {str(result)}"
                        )
            except asyncio.TimeoutError:
                self.logger.warning(f"[{self.name}] Timeout waiting for {len(self._tasks)} tasks to cleanup")

            self._tasks.clear()

    async def _cleanup_sync_worker(self):
        """Clean up sync worker and pending users."""
        # Cancel sync worker if running
        async with self._sync_worker_lock:
            if self._sync_worker_task and not self._sync_worker_task.done():
                self._sync_worker_task.cancel()
                try:
                    await asyncio.wait_for(self._sync_worker_task, timeout=2.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
                self._sync_worker_task = None

        # Clear pending users
        async with self._pending_users_lock:
            self._pending_users.clear()
            self._work_available.clear()

    def is_shutting_down(self) -> bool:
        """Check if the node is shutting down"""
        return self._shutdown_event.is_set()

    async def _ensure_sync_worker_running(self):
        """Spawn sync worker if not already running."""
        async with self._sync_worker_lock:
            if self._sync_worker_task is None or self._sync_worker_task.done():
                self._sync_worker_task = asyncio.create_task(self._sync_worker())

    async def _drain_pending_users(self) -> list[User]:
        """Atomically get and clear all pending users."""
        async with self._pending_users_lock:
            if not self._pending_users:
                self._work_available.clear()
                return []
            users = list(self._pending_users.values())
            self._pending_users.clear()
            self._work_available.clear()
            return users

    async def _requeue_failed_users(self, users: list[User]):
        """Re-queue users that failed to sync (only if not already pending)."""
        async with self._pending_users_lock:
            for user in users:
                # Only re-queue if there's no newer version pending
                if user.email not in self._pending_users:
                    self._pending_users[user.email] = user
            if self._pending_users:
                self._work_available.set()

    async def _sync_worker(self):
        """Lazy worker that processes pending users and exits when idle."""
        self.logger.debug(f"[{self.name}] Sync worker started")
        retry_delay = 1.0
        max_retry_delay = 30.0

        try:
            while not self.is_shutting_down():
                # Wait for work or timeout
                try:
                    await asyncio.wait_for(self._work_available.wait(), timeout=self._worker_idle_timeout)
                except asyncio.TimeoutError:
                    # No work for idle_timeout seconds, exit worker
                    self.logger.debug(f"[{self.name}] Sync worker idle, exiting")
                    break

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

                # Drain all pending users atomically (only when healthy)
                users = await self._drain_pending_users()
                if not users:
                    continue

                # Batch sync users individually
                try:
                    failed_users = await self._sync_batch_users(users)
                    if failed_users:
                        self.logger.warning(f"[{self.name}] {len(failed_users)}/{len(users)} users failed to sync")
                        await self._requeue_failed_users(failed_users)
                        await self._increment_user_sync_failure()
                        # Exponential backoff on partial failure
                        await asyncio.sleep(retry_delay)
                        retry_delay = min(retry_delay * 2, max_retry_delay)
                    else:
                        self.logger.debug(f"[{self.name}] Synced {len(users)} user(s)")
                        await self._reset_user_sync_failure_count()
                        retry_delay = 1.0  # Reset retry delay on success

                except Exception as e:
                    error_type = type(e).__name__
                    self.logger.warning(
                        f"[{self.name}] Batch sync failed for {len(users)} user(s), requeuing | "
                        f"Error: {error_type} - {str(e)}"
                    )
                    await self._increment_user_sync_failure()
                    await self._requeue_failed_users(users)
                    # Exponential backoff on failure
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, max_retry_delay)

        except asyncio.CancelledError:
            self.logger.debug(f"[{self.name}] Sync worker cancelled")
        except Exception as e:
            error_type = type(e).__name__
            self.logger.error(
                f"[{self.name}] Unexpected error in sync worker | Error: {error_type} - {str(e)}", exc_info=True
            )
        finally:
            self.logger.debug(f"[{self.name}] Sync worker finished")

    async def _make_json_request(
        self,
        method: str,
        endpoint: str,
        timeout: Optional[int] = None,
        json: Optional[dict] = None,
    ) -> httpx.Response:
        """Make an HTTP request to the node's REST API."""
        if timeout is None:
            timeout = self._default_timeout

        try:
            response = await self._json_client.request(
                method=method,
                url=endpoint,
                json=json,
                timeout=httpx.Timeout(timeout, connect=timeout, read=timeout, write=timeout),
            )
            response.raise_for_status()
            return response

        except httpx.HTTPStatusError as e:
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

        except httpx.RequestError as e:
            raise NodeAPIError(code=-5, detail=f"Request error: {str(e)}") from e

    async def check_connectivity(self) -> bool:
        """Check if the node service is reachable via its REST API."""
        try:
            response = await self._make_json_request(method="GET", endpoint="/", timeout=5)
            return response.status_code == 200
        except NodeAPIError as e:
            self.logger.error(f"[{self.name}] Connectivity check failed: {str(e)}")
            return False

    async def update_node(self) -> httpx.Response:
        """Trigger a node update via the REST API."""

        if not (await self.check_connectivity()):
            raise NodeAPIError(code=503, detail="Node service is not reachable")
        response = await self._make_json_request(method="POST", endpoint="/node/update")
        return response

    async def update_core(self, json: dict) -> httpx.Response:
        """Trigger a node core update via the REST API."""

        if not (await self.check_connectivity()):
            raise NodeAPIError(code=503, detail="Node service is not reachable")
        response = await self._make_json_request(method="POST", endpoint="/node/core_update", json=json)
        return response

    async def update_geofiles(self, json: dict) -> httpx.Response:
        """Trigger a node geofiles update via the REST API."""

        if not (await self.check_connectivity()):
            raise NodeAPIError(code=503, detail="Node service is not reachable")
        response = await self._make_json_request(method="POST", endpoint="/node/geofiles", json=json)
        return response
