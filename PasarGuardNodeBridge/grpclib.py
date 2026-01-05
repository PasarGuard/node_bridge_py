import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from grpclib.client import Channel, Stream
from grpclib.config import Configuration
from grpclib.exceptions import GRPCError, StreamTerminatedError

from PasarGuardNodeBridge.abstract_node import PasarGuardNode
from PasarGuardNodeBridge.common import service_grpc
from PasarGuardNodeBridge.common import service_pb2 as service
from PasarGuardNodeBridge.controller import Health, NodeAPIError
from PasarGuardNodeBridge.utils import format_host_for_url, grpc_to_http_status


DEFAULT_GRPC_MAX_MESSAGE_BYTES = 4 * 1024 * 1024  # Default gRPC max receive size (4MB)


class Node(PasarGuardNode):
    def __init__(
        self,
        address: str,
        port: int,
        api_port: int,
        server_ca: str,
        api_key: str,
        name: str = "default",
        extra: dict | None = None,
        logger: logging.Logger | None = None,
        default_timeout: int = 10,
        internal_timeout: int = 15,
        max_message_size: int | None = None,
        **kwargs,
    ):
        host_for_url = format_host_for_url(address)
        service_url = f"https://{host_for_url}:{api_port}/"
        super().__init__(server_ca, api_key, service_url, name, extra, logger, default_timeout, internal_timeout)

        try:
            # Default gRPC max receive size is 4MB; allow override but use this value to size flow-control windows
            if max_message_size is None:
                max_message_size = DEFAULT_GRPC_MAX_MESSAGE_BYTES
            self._max_message_size = max_message_size
            self.channel = Channel(
                host=address,
                port=port,
                ssl=self.ctx,
                # Align HTTP/2 windows with desired message size; grpclib doesn't expose grpc.options
                config=Configuration(
                    _keepalive_timeout=10,
                    http2_connection_window_size=max_message_size,
                    http2_stream_window_size=max_message_size,
                ),
            )
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

        # Build backend payload and check if it fits under the configured gRPC message cap
        users_to_stream_after_start: list[service.User] = []
        req = service.Backend(
            type=backend_type,
            config=config,
            users=users,
            keep_alive=keep_alive,
            exclude_inbounds=exclude_inbounds,
        )
        req_size = req.ByteSize()

        if req_size > self._max_message_size:
            # Try sending without users and stream them after start to avoid oversized unary payloads
            users_to_stream_after_start = list(users)
            original_req_size = req_size
            req = service.Backend(
                type=backend_type,
                config=config,
                users=[],
                keep_alive=keep_alive,
                exclude_inbounds=exclude_inbounds,
            )
            req_size = req.ByteSize()
            if req_size > self._max_message_size:
                raise NodeAPIError(
                    code=413,
                    detail=(
                        f"Start payload is {req_size} bytes which exceeds gRPC max message size "
                        f"({self._max_message_size} bytes). Reduce config size or increase server limits."
                    ),
                )
            self.logger.warning(
                f"[{self.name}] Start payload {original_req_size} bytes exceeds max "
                f"{self._max_message_size} bytes; sending config-only payload ({req_size} bytes) "
                f"and streaming {len(users_to_stream_after_start)} user(s) after start"
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
                await self.connect(info.node_version, info.core_version)
                if users_to_stream_after_start:
                    failed_users = await self._sync_batch_users(users_to_stream_after_start)
                    if failed_users:
                        raise NodeAPIError(
                            500,
                            f"Failed to sync {len(failed_users)}/{len(users_to_stream_after_start)} users after start",
                        )
            except Exception as e:
                await self.disconnect()
                if isinstance(e, NodeAPIError):
                    raise
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
        self, users: list[service.User], flush_pending: bool = False, timeout: int | None = None
    ) -> service.Empty | None:
        timeout = timeout or self._default_timeout
        if flush_pending:
            await self.flush_pending_users()

        users_payload = service.Users(users=users)
        payload_size = users_payload.ByteSize()

        async with self._node_lock:
            if payload_size > self._max_message_size:
                self.logger.warning(
                    f"[{self.name}] SyncUsers payload {payload_size} bytes exceeds max "
                    f"{self._max_message_size} bytes; using streaming SyncUser instead"
                )
                failed_users = await self._sync_batch_users(users)
                if failed_users:
                    raise NodeAPIError(
                        500,
                        f"Failed to sync {len(failed_users)}/{len(users)} users via streaming",
                    )
                return service.Empty()

            return await self._handle_grpc_request(
                method=self._client.SyncUsers,
                request=users_payload,
                timeout=timeout,
            )

    async def _sync_batch_users(self, users: list[service.User]) -> list[service.User]:
        """Sync users via gRPC SyncUser stream. Returns failed users."""
        failed = []
        try:
            async with self._client.SyncUser.open(metadata=self._metadata) as stream:
                for user in users:
                    try:
                        await asyncio.wait_for(stream.send_message(user), timeout=self._internal_timeout)
                    except Exception as e:
                        error_type = type(e).__name__
                        self.logger.warning(
                            f"[{self.name}] Failed to sync user {user.email} | Error: {error_type} - {str(e)}"
                        )
                        failed.append(user)
                await stream.end()
        except Exception as e:
            # Stream-level failure - all users failed
            error_type = type(e).__name__
            self.logger.error(f"[{self.name}] Stream failed | Error: {error_type} - {str(e)}")
            return users
        return failed

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
