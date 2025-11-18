import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from grpclib.client import Channel, Stream
from grpclib.config import Configuration
from grpclib.exceptions import GRPCError, StreamTerminatedError

from PasarGuardNodeBridge.common import service_pb2 as service
from PasarGuardNodeBridge.common import service_grpc
from PasarGuardNodeBridge.controller import NodeAPIError, Health
from PasarGuardNodeBridge.abstract_node import PasarGuardNode
from PasarGuardNodeBridge.utils import grpc_to_http_status


class Node(PasarGuardNode):
    def __init__(
        self,
        address: str,
        port: int,
        server_ca: str,
        api_key: str,
        name: str = "default",
        extra: dict | None = None,
        logger: logging.Logger | None = None,
        default_timeout: int = 10,
        internal_timeout: int = 15,
    ):
        super().__init__(server_ca, api_key, name, extra, logger, default_timeout, internal_timeout)

        try:
            self.channel = Channel(host=address, port=port, ssl=self.ctx, config=Configuration(_keepalive_timeout=10))
            self._client = service_grpc.NodeServiceStub(self.channel)
            self._metadata = {"x-api-key": api_key}
        except Exception as e:
            raise NodeAPIError(-1, f"Channel initialization failed: {str(e)}")

        self._node_lock = asyncio.Lock()

    def _close_chan(self):
        """Close gRPC channel"""
        if hasattr(self, "channel"):
            try:
                self.channel.close()
            except Exception:
                pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.stop()
        self._close_chan()

    def __del__(self):
        self._close_chan()

    def _handle_error(self, error: Exception):
        """Convert gRPC errors to NodeAPIError with HTTP status codes."""
        if isinstance(error, asyncio.TimeoutError):
            raise NodeAPIError(-1, "Request timed out")
        elif isinstance(error, GRPCError):
            http_status = grpc_to_http_status(error.status)
            message = error.message or "Unknown gRPC error"
            raise NodeAPIError(http_status, message)
        elif isinstance(error, StreamTerminatedError):
            raise NodeAPIError(-1, f"Stream terminated: {str(error)}")
        else:
            raise NodeAPIError(0, str(error))

    async def _handle_grpc_request(self, method, request, timeout: int | None = None):
        """Handle a gRPC request and convert errors to NodeAPIError."""
        timeout = timeout or self._internal_timeout
        try:
            return await asyncio.wait_for(method(request, metadata=self._metadata), timeout=timeout)
        except Exception as e:
            self._handle_error(e)

    async def start(
        self,
        config: str,
        backend_type: service.BackendType,
        users: list[service.User],
        keep_alive: int = 0,
        exclude_inbounds: list[str] = [],
        timeout: int | None = None,
    ) -> service.BaseInfoResponse | None:
        """Start the node with proper task management"""
        timeout = timeout or self._default_timeout
        health = await self.get_health()
        if health is Health.INVALID:
            raise NodeAPIError(code=-4, detail="Invalid node")

        req = service.Backend(
            type=backend_type, config=config, users=users, keep_alive=keep_alive, exclude_inbounds=exclude_inbounds
        )

        async with self._node_lock:
            info: service.BaseInfoResponse = await self._handle_grpc_request(
                method=self._client.Start,
                request=req,
                timeout=timeout,
            )

            if not info.started:
                raise NodeAPIError(500, "Failed to start the node")

            try:
                tasks = [self._sync_user]
                await self.connect(info.node_version, info.core_version, tasks)
            except Exception as e:
                await self.disconnect()
                self._handle_error(e)

            return info

    async def stop(self, timeout: int | None = None) -> None:
        """Stop the node with proper cleanup"""
        timeout = timeout or self._default_timeout
        if await self.get_health() is Health.NOT_CONNECTED:
            return

        async with self._node_lock:
            await self.disconnect()

            try:
                await self._handle_grpc_request(
                    method=self._client.Stop,
                    request=service.Empty(),
                    timeout=timeout,
                )
            except Exception:
                pass

    async def info(self, timeout: int | None = None) -> service.BaseInfoResponse | None:
        timeout = timeout or self._default_timeout
        return await self._handle_grpc_request(
            method=self._client.GetBaseInfo,
            request=service.Empty(),
            timeout=timeout,
        )

    async def get_system_stats(self, timeout: int | None = None) -> service.SystemStatsResponse | None:
        timeout = timeout or self._default_timeout
        return await self._handle_grpc_request(
            method=self._client.GetSystemStats,
            request=service.Empty(),
            timeout=timeout,
        )

    async def get_backend_stats(self, timeout: int | None = None) -> service.BackendStatsResponse | None:
        timeout = timeout or self._default_timeout
        return await self._handle_grpc_request(
            method=self._client.GetBackendStats,
            request=service.Empty(),
            timeout=timeout,
        )

    async def get_stats(
        self, stat_type: service.StatType, reset: bool = True, name: str = "", timeout: int | None = None
    ) -> service.StatResponse | None:
        timeout = timeout or self._default_timeout
        return await self._handle_grpc_request(
            method=self._client.GetStats,
            request=service.StatRequest(reset=reset, name=name, type=stat_type),
            timeout=timeout,
        )

    async def get_user_online_stats(self, email: str, timeout: int | None = None) -> service.OnlineStatResponse | None:
        timeout = timeout or self._default_timeout
        return await self._handle_grpc_request(
            method=self._client.GetUserOnlineStats,
            request=service.StatRequest(name=email),
            timeout=timeout,
        )

    async def get_user_online_ip_list(
        self, email: str, timeout: int | None = None
    ) -> service.StatsOnlineIpListResponse | None:
        timeout = timeout or self._default_timeout
        return await self._handle_grpc_request(
            method=self._client.GetUserOnlineIpListStats,
            request=service.StatRequest(name=email),
            timeout=timeout,
        )

    async def sync_users(
        self, users: list[service.User], flush_queue: bool = False, timeout: int | None = None
    ) -> service.Empty | None:
        timeout = timeout or self._default_timeout
        if flush_queue:
            await self.flush_user_queue()

        async with self._node_lock:
            return await self._handle_grpc_request(
                method=self._client.SyncUsers,
                request=service.Users(users=users),
                timeout=timeout,
            )

    async def _sync_user_with_retry(
        self,
        stream: Stream[service.User, service.Empty],
        user: service.User,
        max_retries: int = 3,
        timeout: int | None = None,
    ) -> tuple[bool, bool]:
        """
        Attempt to sync a user via gRPC stream with retry logic for timeout errors.
        Returns (success, is_timeout_error) tuple.
        
        Raises StreamTerminatedError or GRPCError if stream is closed/invalid.
        These should cause the stream to be reopened.
        """
        timeout = timeout or self._internal_timeout
        for attempt in range(max_retries):
            try:
                # Check if stream is still valid before attempting to send
                # If stream is closed, send_message will raise StreamTerminatedError immediately
                await asyncio.wait_for(stream.send_message(user), timeout=timeout)
                return True, False
            except (StreamTerminatedError, GRPCError) as e:
                # Stream errors - these indicate the stream is closed/invalid
                # Re-raise to signal that stream needs to be reopened
                error_type = type(e).__name__
                self.logger.warning(
                    f"[{self.name}] Stream error syncing user {user.email} | "
                    f"Error: {error_type} - {str(e)}"
                )
                raise  # Re-raise to cause stream processing to fail and stream to be reopened
            except asyncio.TimeoutError:
                # Retry on timeout
                if attempt < max_retries - 1:
                    self.logger.debug(
                        f"[{self.name}] Timeout syncing user {user.email}, retry {attempt + 1}/{max_retries} | "
                        f"Error: TimeoutError"
                    )
                    await asyncio.sleep(0.5)
                    continue
                # Last attempt failed
                self.logger.warning(
                    f"[{self.name}] Failed to sync user {user.email} after {max_retries} timeout attempts"
                )
                return False, True
            except Exception as e:
                # Other errors, don't retry
                error_type = type(e).__name__
                self.logger.error(
                    f"[{self.name}] Unexpected error syncing user {user.email} | " f"Error: {error_type} - {str(e)}",
                    exc_info=True,
                )
                return False, False
        return False, True  # If we exhaust retries, it was timeout

    async def _check_node_health(self):
        """Health check task with proper cancellation handling"""
        health_check_interval = 10
        max_retries = 3
        retry_delay = 2
        retries = 0
        self.logger.debug(f"[{self.name}] Health check task started")

        try:
            while not self.is_shutting_down():
                last_health = await self.get_health()

                if last_health in (Health.NOT_CONNECTED, Health.INVALID):
                    self.logger.debug(f"[{self.name}] Health check task stopped due to node state: {last_health.name}")
                    return

                try:
                    await asyncio.wait_for(self.get_backend_stats(), timeout=10)
                    # Only update to HEALTHY if we were BROKEN or NOT_CONNECTED
                    if last_health in (Health.BROKEN, Health.NOT_CONNECTED):
                        self.logger.debug(f"[{self.name}] Node health is HEALTHY")
                        await self.set_health(Health.HEALTHY)
                    retries = 0
                except Exception as e:
                    retries += 1
                    error_type = type(e).__name__
                    if retries >= max_retries:
                        if last_health != Health.BROKEN:
                            self.logger.error(
                                f"[{self.name}] Health check failed after {max_retries} retries, setting health to BROKEN | "
                                f"Error: {error_type} - {str(e)}"
                            )
                            await self.set_health(Health.BROKEN)
                    else:
                        self.logger.warning(
                            f"[{self.name}] Health check failed, retry {retries}/{max_retries} in {retry_delay}s | "
                            f"Error: {error_type} - {str(e)}"
                        )
                        await asyncio.sleep(retry_delay)
                        continue

                try:
                    await asyncio.wait_for(asyncio.sleep(health_check_interval), timeout=health_check_interval + 1)
                except asyncio.TimeoutError:
                    continue

        except asyncio.CancelledError:
            self.logger.debug(f"[{self.name}] Health check task cancelled")
        except Exception as e:
            error_type = type(e).__name__
            self.logger.error(
                f"[{self.name}] Unexpected error in health check task | Error: {error_type} - {str(e)}", exc_info=True
            )
            try:
                await self.set_health(Health.BROKEN)
            except Exception as e_set_health:
                error_type_set = type(e_set_health).__name__
                self.logger.error(
                    f"[{self.name}] Failed to set health to BROKEN | Error: {error_type_set} - {str(e_set_health)}",
                    exc_info=True,
                )
        finally:
            self.logger.debug(f"[{self.name}] Health check task finished")

    async def _should_continue_task(self, task_name: str) -> bool:
        """Check if task should continue based on health status.

        Returns:
            True if task should continue, False if task should stop
        """
        health = await self.get_health()

        # Stop for NOT_CONNECTED and INVALID (INVALID means instance is being deleted)
        if health in (Health.NOT_CONNECTED, Health.INVALID):
            self.logger.debug(f"[{self.name}] {task_name} stopped due to node state: {health.name}")
            return False
        return True

    async def _wait_for_healthy_state(self, retry_delay: float, max_retry_delay: float) -> float:
        """Wait if node is broken, returns updated retry_delay."""
        health = await self.get_health()

        if health == Health.BROKEN:
            self.logger.warning(f"[{self.name}] Node is broken, waiting for {retry_delay} seconds")
            try:
                await asyncio.wait_for(asyncio.sleep(retry_delay), timeout=retry_delay + 1)
            except asyncio.TimeoutError:
                pass
            return min(retry_delay * 1.5, max_retry_delay)
        return retry_delay

    async def _get_user_queues(self):
        """Get user and notify queue references with minimal lock scope.

        Returns:
            Tuple of (user_queue, notify_queue) or (None, None) if unavailable
        """
        async with self._queue_lock:
            return self._user_queue, self._notify_queue

    async def _wait_for_user_or_notification(self, user_queue, notify_queue):
        """Wait for either a user or notification from queues.

        Returns:
            Tuple of (done_tasks, user_task, notify_task)
        """
        user_task = asyncio.create_task(user_queue.get())
        notify_task = asyncio.create_task(notify_queue.get())

        try:
            done, pending = await asyncio.wait(
                [user_task, notify_task], return_when=asyncio.FIRST_COMPLETED, timeout=30
            )

            # Cancel pending tasks
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            return done, user_task, notify_task
        except asyncio.CancelledError:
            # If we're cancelled, make sure to cancel both tasks
            user_task.cancel()
            notify_task.cancel()
            try:
                await asyncio.gather(user_task, notify_task, return_exceptions=True)
            except Exception:
                pass
            raise

    async def _handle_user_sync_stream(self, stream, user, sync_retry_delay: float, max_retry_delay: float) -> float:
        """Sync a single user via gRPC stream and handle retry logic.

        Returns:
            Updated sync_retry_delay
        """
        self.logger.debug(f"[{self.name}] Syncing user {user.email}")
        success, is_timeout = await self._sync_user_with_retry(
            stream, user, max_retries=3, timeout=self._internal_timeout
        )

        if success:
            self.logger.debug(f"[{self.name}] Successfully synced user {user.email}")
            await self._reset_user_sync_failure_count()
            return 1.0
        else:
            # Only increment hard reset counter for non-timeout errors
            if not is_timeout:
                await self._increment_user_sync_failure()

            self.logger.warning(f"[{self.name}] Failed to sync user {user.email}, requeueing")
            await self.requeue_user_with_deduplication(user)
            try:
                await asyncio.wait_for(asyncio.sleep(sync_retry_delay), timeout=sync_retry_delay + 1)
            except asyncio.TimeoutError:
                pass
            return min(sync_retry_delay * 2, max_retry_delay)

    async def _process_user_sync_stream(self, stream, sync_retry_delay: float, max_retry_delay: float):
        """Process user sync stream, handling users and notifications.

        Returns:
            Tuple of (stream_failed, updated_sync_retry_delay)
        """
        while not self.is_shutting_down():
            try:
                # Get queue references
                user_queue, notify_queue = await self._get_user_queues()

                if user_queue is None or notify_queue is None:
                    return True, sync_retry_delay

                # Wait for user or notification
                done, user_task, notify_task = await self._wait_for_user_or_notification(user_queue, notify_queue)

                if not done:
                    continue

                # Handle notification
                if notify_task in done:
                    notify_result = notify_task.result()
                    if notify_result is None:
                        self.logger.debug(f"[{self.name}] Received notification to renew queues, breaking stream")
                        return True, sync_retry_delay
                    continue

                # Handle user sync
                if user_task in done:
                    user = user_task.result()
                    if user is None:
                        self.logger.debug(f"[{self.name}] Received None user, breaking stream to renew queues")
                        return True, sync_retry_delay

                    sync_retry_delay = await self._handle_user_sync_stream(
                        stream, user, sync_retry_delay, max_retry_delay
                    )

            except asyncio.CancelledError:
                self.logger.debug(f"[{self.name}] User sync stream processing cancelled")
                raise
            except Exception as e:
                error_type = type(e).__name__
                self.logger.error(
                    f"[{self.name}] Error in user sync stream | Error: {error_type} - {str(e)}", exc_info=True
                )
                # Increment failure counter for stream processing errors
                await self._increment_user_sync_failure()
                try:
                    await asyncio.wait_for(asyncio.sleep(sync_retry_delay), timeout=sync_retry_delay + 1)
                except asyncio.TimeoutError:
                    pass
                sync_retry_delay = min(sync_retry_delay * 2, max_retry_delay)
                # Return True to indicate stream failed
                return True, sync_retry_delay

        return False, sync_retry_delay

    async def _open_and_process_user_sync_stream(self, sync_retry_delay: float, max_retry_delay: float):
        """Open user sync stream and process it.

        Returns:
            Tuple of (stream_failed, updated_sync_retry_delay)
        """
        try:
            self.logger.debug(f"[{self.name}] Opening user sync stream")
            async with self._client.SyncUser.open(metadata=self._metadata) as stream:
                self.logger.debug(f"[{self.name}] User sync stream opened successfully")
                # Stream opened successfully - reset failure counter (this also clears hard reset event)
                await self._reset_user_sync_failure_count()

                # Process the stream
                stream_failed, sync_retry_delay = await self._process_user_sync_stream(stream, 1.0, max_retry_delay)

                # If stream processed successfully (not failed), reset failure counter again
                if not stream_failed:
                    await self._reset_user_sync_failure_count()

                return stream_failed, sync_retry_delay
        except asyncio.CancelledError:
            self.logger.debug(f"[{self.name}] User sync stream cancelled")
            raise
        except Exception as e:
            error_type = type(e).__name__
            self.logger.error(f"[{self.name}] User sync stream failed | Error: {error_type} - {str(e)}", exc_info=True)
            # Increment failure counter for stream-level errors
            await self._increment_user_sync_failure()
            return True, sync_retry_delay

    async def _sync_user(self):
        """User sync task with proper cancellation handling"""
        retry_delay = 10.0
        max_retry_delay = 60.0
        sync_retry_delay = 1.0
        self.logger.debug(f"[{self.name}] User sync task started")

        try:
            while not self.is_shutting_down():
                if not await self._should_continue_task("User sync task"):
                    break

                health = await self.get_health()
                was_broken = health == Health.BROKEN
                # If BROKEN, wait longer but still try to sync to allow recovery
                if was_broken:
                    retry_delay = await self._wait_for_healthy_state(retry_delay, max_retry_delay)
                    # Use longer delay when BROKEN, but still attempt sync
                    sync_retry_delay = max(sync_retry_delay, 5.0)

                # Reset retry delay when healthy
                if health != Health.BROKEN:
                    retry_delay = 10.0

                # Try to open and process user sync stream
                stream_failed, sync_retry_delay = await self._open_and_process_user_sync_stream(
                    sync_retry_delay, max_retry_delay
                )

                if stream_failed:
                    self.logger.warning(
                        f"[{self.name}] User sync stream failed, retrying in {sync_retry_delay} seconds"
                    )
                    try:
                        await asyncio.wait_for(asyncio.sleep(sync_retry_delay), timeout=sync_retry_delay + 1)
                    except asyncio.TimeoutError:
                        pass
                    sync_retry_delay = min(sync_retry_delay * 2, max_retry_delay)
                else:
                    # Stream succeeded - reset retry delay and ensure health is updated
                    sync_retry_delay = 1.0
                    # Try to recover health if it was BROKEN
                    # INVALID nodes should not recover (instance is being deleted)
                    recovery_delays = await self._try_recover_health_after_sync(was_broken, False)
                    if recovery_delays[0] is not None:
                        retry_delay, _ = recovery_delays

        except asyncio.CancelledError:
            self.logger.debug(f"[{self.name}] User sync task cancelled")
        finally:
            self.logger.debug(f"[{self.name}] User sync task finished")

    @asynccontextmanager
    async def stream_logs(self, max_queue_size: int = 1000) -> AsyncIterator[asyncio.Queue]:
        """Context manager for streaming logs on-demand.

        Yields a queue that receives log messages in real-time.
        The stream is automatically closed when the context exits.

        IMPORTANT: When an error occurs during log streaming, a NodeAPIError instance
        is placed in the queue. You must check the type of each item received from
        the queue and raise it if it's an error.

        Args:
            max_queue_size: Maximum size of the log queue

        Yields:
            asyncio.Queue containing log messages (str) or NodeAPIError on failure

        Raises:
            NodeAPIError: If the stream fails to open or encounters errors during operation

        Example:
            try:
                async with node.stream_logs() as log_queue:
                    while True:
                        item = await log_queue.get()
                        # Check if we received an error
                        if isinstance(item, NodeAPIError):
                            raise item
                        # Process the log message
                        print(f"LOG: {item}")
            except NodeAPIError as e:
                print(f"Log stream failed: {e.code} - {e.detail}")
                # Reconnect or handle error
        """
        log_queue: asyncio.Queue[str | NodeAPIError] = asyncio.Queue(maxsize=max_queue_size)
        stream_task = None

        async def _receive_logs(stream: Stream[service.Empty, service.Log]):
            """Receive log messages and put them in the queue."""
            try:
                while True:
                    log = await stream.recv_message()
                    if log is None:
                        break

                    try:
                        await log_queue.put(log.detail)
                    except asyncio.QueueFull:
                        # Drop oldest log if queue is full
                        try:
                            log_queue.get_nowait()
                            await log_queue.put(log.detail)
                        except (asyncio.QueueEmpty, asyncio.QueueFull):
                            pass
            except asyncio.CancelledError:
                self.logger.debug(f"[{self.name}] Log stream receive task cancelled")
                raise
            except StreamTerminatedError as e:
                # Stream was cancelled intentionally, this is expected during cleanup
                self.logger.debug(f"[{self.name}] Log stream terminated: {str(e)}")
            except Exception as e:
                error_type = type(e).__name__
                self.logger.error(f"[{self.name}] Error receiving logs | Error: {error_type} - {str(e)}")
                # Convert exception to NodeAPIError and put directly into log queue
                # so user gets immediate notification when reading
                try:
                    self._handle_error(e)
                except NodeAPIError as api_error:
                    try:
                        # Put error into log queue for immediate detection
                        log_queue.put_nowait(api_error)
                    except asyncio.QueueFull:
                        pass

        try:
            self.logger.debug(f"[{self.name}] Opening on-demand log stream")
            async with self._client.GetLogs.open(metadata=self._metadata) as stream:
                await stream.send_message(service.Empty())
                self.logger.debug(f"[{self.name}] On-demand log stream opened successfully")

                # Start background task to receive logs
                stream_task = asyncio.create_task(_receive_logs(stream))

                try:
                    # Yield the queue to the caller
                    yield log_queue

                    # After context exits, check if background task failed
                    if stream_task.done():
                        exc = stream_task.exception()
                        if exc and not isinstance(exc, asyncio.CancelledError):
                            self._handle_error(exc)
                finally:
                    # Cleanup: cancel the gRPC stream to unblock recv_message()
                    try:
                        await stream.cancel()
                    except Exception:
                        pass

                    # Then cancel and wait for background task to finish
                    if stream_task and not stream_task.done():
                        stream_task.cancel()
                        try:
                            await asyncio.wait_for(stream_task, timeout=1.0)
                        except (asyncio.CancelledError, asyncio.TimeoutError):
                            pass

        except NodeAPIError:
            # Already a NodeAPIError, re-raise as-is
            raise
        except Exception as e:
            error_type = type(e).__name__
            self.logger.error(f"[{self.name}] Failed to open log stream | Error: {error_type} - {str(e)}")
            # Convert to NodeAPIError
            self._handle_error(e)
        finally:
            self.logger.debug(f"[{self.name}] On-demand log stream closed")
