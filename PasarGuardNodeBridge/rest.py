import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from google.protobuf.message import DecodeError, Message

from PasarGuardNodeBridge.abstract_node import PasarGuardNode
from PasarGuardNodeBridge.common import service_pb2 as service
from PasarGuardNodeBridge.controller import Health, NodeAPIError
from PasarGuardNodeBridge.utils import format_host_for_url


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
        streaming_threshold: int = 1 * 1024 * 1024,
        streaming_chunk_size: int = 64 * 1024,
        **kwargs,
    ):
        host_for_url = format_host_for_url(address)
        service_url = f"https://{host_for_url}:{api_port}/"
        super().__init__(server_ca, api_key, service_url, name, extra, logger, default_timeout, internal_timeout)

        url = f"https://{host_for_url}:{port}/"
        self._client = httpx.AsyncClient(
            http2=True,
            verify=self.ctx,
            headers={"Content-Type": "application/x-protobuf", "x-api-key": api_key},
            base_url=url,
            timeout=httpx.Timeout(None),
        )

        self._node_lock = asyncio.Lock()
        self._streaming_threshold = max(streaming_threshold, 0)
        self._streaming_chunk_size = max(streaming_chunk_size, 1)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.stop()
        await self._client.aclose()

    def _serialize_protobuf(self, proto_message: Message) -> bytes:
        """Serialize a protobuf message to bytes."""
        return proto_message.SerializeToString()

    def _deserialize_protobuf(self, proto_class: type[Message], data: bytes) -> Message:
        """Deserialize bytes into a protobuf message."""
        proto_instance = proto_class()
        try:
            proto_instance.ParseFromString(data)
        except DecodeError as e:
            raise NodeAPIError(code=-2, detail=f"Error deserialising protobuf: {e}")
        return proto_instance

    def _handle_error(self, error: Exception):
        if isinstance(error, httpx.RemoteProtocolError):
            raise NodeAPIError(code=500, detail=f"Server closed connection: {error}")
        elif isinstance(error, httpx.ConnectError):
            # Connection errors (connection refused, DNS errors, etc.) are NOT timeouts
            raise NodeAPIError(code=-2, detail=f"Connection error: {error}")
        elif isinstance(error, httpx.NetworkError):
            # Other network errors (not connection errors or timeouts) are NOT timeouts
            raise NodeAPIError(code=-2, detail=f"Network error: {error}")
        elif isinstance(error, httpx.TimeoutException):
            # Only actual timeouts should be classified as timeout errors
            raise NodeAPIError(code=-1, detail=f"Timeout error: {error}")
        elif isinstance(error, httpx.HTTPStatusError):
            raise NodeAPIError(code=error.response.status_code, detail=f"HTTP error: {error.response.text}")
        else:
            raise NodeAPIError(0, str(error))

    def _should_stream(self, payload: bytes) -> bool:
        """Decide whether to stream a payload instead of sending it in one chunk."""
        return len(payload) >= self._streaming_threshold > 0

    def _stream_payload(self, payload: bytes):
        """Yield payload in small chunks for chunked transfer."""

        async def _gen():
            view = memoryview(payload)
            total = len(view)
            chunk_size = self._streaming_chunk_size
            for start in range(0, total, chunk_size):
                yield bytes(view[start : start + chunk_size])

        return _gen()

    async def _make_request(
        self,
        method: str,
        endpoint: str,
        timeout: int,
        proto_message: Message = None,
        proto_response_class: type[Message] = None,
    ) -> Message:
        """Handle common REST API call logic with protobuf support (async)."""
        request_data = None

        if proto_message:
            payload = self._serialize_protobuf(proto_message)
            request_data = self._stream_payload(payload) if self._should_stream(payload) else payload

        try:
            # Convert integer timeout to httpx.Timeout object for explicit timeout configuration
            # This ensures connection, read, and write timeouts are all set properly
            httpx_timeout = httpx.Timeout(timeout, connect=timeout, read=timeout, write=timeout)
            response = await self._client.request(
                method=method,
                url=endpoint,
                content=request_data,
                timeout=httpx_timeout,
            )
            response.raise_for_status()

            if proto_response_class:
                return self._deserialize_protobuf(proto_response_class, response.content)
            return response.content

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
    ):
        """Start the node with proper task management"""
        timeout = timeout or self._default_timeout
        health = await self.get_health()
        if health is Health.INVALID:
            raise NodeAPIError(code=-4, detail="Invalid node")

        async with self._node_lock:
            response: service.BaseInfoResponse = await self._make_request(
                method="POST",
                endpoint="start",
                timeout=timeout,
                proto_message=service.Backend(
                    type=backend_type,
                    config=config,
                    users=users,
                    keep_alive=keep_alive,
                    exclude_inbounds=exclude_inbounds,
                ),
                proto_response_class=service.BaseInfoResponse,
            )

            if not response.started:
                raise NodeAPIError(500, "Failed to start the node")

            try:
                await self.connect(response.node_version, response.core_version)
            except Exception as e:
                await self.disconnect()
                self._handle_error(e)

        return response

    async def stop(self, timeout: int | None = None) -> None:
        """Stop the node with proper cleanup"""
        timeout = timeout or self._default_timeout
        if await self.get_health() is Health.NOT_CONNECTED:
            return

        async with self._node_lock:
            await self.disconnect()

            try:
                await self._make_request(method="PUT", endpoint="stop", timeout=timeout)
            except Exception:
                pass

    async def info(self, timeout: int | None = None) -> service.BaseInfoResponse | None:
        timeout = timeout or self._default_timeout
        return await self._make_request(
            method="GET", endpoint="info", timeout=timeout, proto_response_class=service.BaseInfoResponse
        )

    async def get_system_stats(self, timeout: int | None = None) -> service.SystemStatsResponse | None:
        timeout = timeout or self._default_timeout
        return await self._make_request(
            method="GET", endpoint="stats/system", timeout=timeout, proto_response_class=service.SystemStatsResponse
        )

    async def get_backend_stats(self, timeout: int | None = None) -> service.BackendStatsResponse | None:
        timeout = timeout or self._default_timeout
        return await self._make_request(
            method="GET", endpoint="stats/backend", timeout=timeout, proto_response_class=service.BackendStatsResponse
        )

    async def get_stats(
        self, stat_type: service.StatType, reset: bool = True, name: str = "", timeout: int | None = None
    ) -> service.StatResponse | None:
        timeout = timeout or self._default_timeout
        return await self._make_request(
            method="GET",
            endpoint="stats",
            timeout=timeout,
            proto_message=service.StatRequest(reset=reset, name=name, type=stat_type),
            proto_response_class=service.StatResponse,
        )

    async def get_user_online_stats(self, email: str, timeout: int | None = None) -> service.OnlineStatResponse | None:
        timeout = timeout or self._default_timeout
        return await self._make_request(
            method="GET",
            endpoint="stats/user/online",
            timeout=timeout,
            proto_message=service.StatRequest(name=email),
            proto_response_class=service.OnlineStatResponse,
        )

    async def get_user_online_ip_list(
        self, email: str, timeout: int | None = None
    ) -> service.StatsOnlineIpListResponse | None:
        timeout = timeout or self._default_timeout
        return await self._make_request(
            method="GET",
            endpoint="stats/user/online_ip",
            timeout=timeout,
            proto_message=service.StatRequest(name=email),
            proto_response_class=service.StatsOnlineIpListResponse,
        )

    async def sync_users(
        self, users: list[service.User], flush_pending: bool = False, timeout: int | None = None
    ) -> service.Empty | None:
        timeout = timeout or self._default_timeout
        if flush_pending:
            await self.flush_pending_users()

        async with self._node_lock:
            return await self._make_request(
                method="PUT",
                endpoint="users/sync",
                timeout=timeout,
                proto_message=service.Users(users=users),
                proto_response_class=service.Empty,
            )

    async def _sync_batch_users(self, users: list[service.User]) -> list[service.User]:
        """Sync users individually via PUT user/sync. Returns failed users."""
        failed = []
        for user in users:
            try:
                await self._make_request(
                    method="PUT",
                    endpoint="user/sync",
                    timeout=self._internal_timeout,
                    proto_message=user,
                    proto_response_class=service.Empty,
                )
            except Exception as e:
                error_type = type(e).__name__
                self.logger.warning(f"[{self.name}] Failed to sync user {user.email} | Error: {error_type} - {str(e)}")
                failed.append(user)
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
                    break

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

        async def _receive_logs(response):
            """Receive log messages from HTTP stream and put them in the queue."""
            try:
                buffer = b""
                async for chunk in response.aiter_bytes():
                    buffer += chunk

                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        line = line.decode().strip()

                        if line:
                            try:
                                await log_queue.put(line)
                            except asyncio.QueueFull:
                                # Drop oldest log if queue is full
                                try:
                                    log_queue.get_nowait()
                                    await log_queue.put(line)
                                except (asyncio.QueueEmpty, asyncio.QueueFull):
                                    pass
            except asyncio.CancelledError:
                self.logger.debug(f"[{self.name}] Log stream receive task cancelled")
                raise
            except (httpx.ReadError, httpx.RemoteProtocolError) as e:
                # Stream was closed intentionally, this is expected during cleanup
                self.logger.debug(f"[{self.name}] Log stream closed: {type(e).__name__}")
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

        response = None
        try:
            self.logger.debug(f"[{self.name}] Opening on-demand log stream")
            response = await self._client.stream("GET", "/logs", timeout=None).__aenter__()
            self.logger.debug(f"[{self.name}] On-demand log stream opened successfully")

            # Start background task to receive logs
            stream_task = asyncio.create_task(_receive_logs(response))

            try:
                # Yield the queue to the caller
                yield log_queue

                # After context exits, check if background task failed
                if stream_task.done():
                    exc = stream_task.exception()
                    if exc and not isinstance(exc, asyncio.CancelledError):
                        self._handle_error(exc)
            finally:
                # Cleanup: Close HTTP stream first to interrupt aiter_bytes()
                if response is not None:
                    try:
                        await response.aclose()
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
