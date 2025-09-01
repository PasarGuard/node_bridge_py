import asyncio

import httpx
from google.protobuf.message import Message, DecodeError

from PasarGuardNodeBridge.controller import NodeAPIError, Health
from PasarGuardNodeBridge.common import service_pb2 as service
from PasarGuardNodeBridge.abstract_node import PasarGuardNode


class Node(PasarGuardNode):
    def __init__(
        self,
        address: str,
        port: int,
        server_ca: str,
        api_key: str,
        extra: dict | None = None,
        max_logs: int = 1000,
    ):
        super().__init__(server_ca, api_key, extra, max_logs)

        url = f"https://{address.strip('/')}:{port}/"

        self._client = httpx.AsyncClient(
            http2=True,
            verify=self.ctx,
            headers={"Content-Type": "application/x-protobuf", "x-api-key": api_key},
            base_url=url,
            timeout=httpx.Timeout(None),
        )

        self._node_lock = asyncio.Lock()

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
            raise NodeAPIError(code=-1, detail=f"Server closed connection: {error}")
        elif isinstance(error, httpx.HTTPStatusError):
            raise NodeAPIError(code=error.response.status_code, detail=f"HTTP error: {error.response.text}")
        elif isinstance(error, httpx.ConnectError) or isinstance(error, httpx.ReadTimeout):
            raise NodeAPIError(code=-1, detail=f"Connection error: {error}")
        else:
            raise NodeAPIError(0, str(error))

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
            request_data = self._serialize_protobuf(proto_message)

        try:
            response = await self._client.request(
                method=method,
                url=endpoint,
                content=request_data,
                timeout=timeout,
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
        ghather_logs: bool = True,
        timeout: int = 15,
    ):
        """Start the node with proper task management"""
        health = await self.get_health()
        if health in (Health.BROKEN, Health.HEALTHY):
            await self.stop()
        elif health is Health.INVALID:
            raise NodeAPIError(code=-4, detail="Invalid node")

        async with self._node_lock:
            response = await self._make_request(
                method="POST",
                endpoint="start",
                timeout=timeout,
                proto_message=service.Backend(type=backend_type, config=config, users=users, keep_alive=keep_alive),
                proto_response_class=service.BaseInfoResponse,
            )

            tasks = []
            try:
                health_task = asyncio.create_task(self._check_node_health(), name=f"health_check_{id(self)}")
                sync_task = asyncio.create_task(self._sync_user(), name=f"sync_user_{id(self)}")
                tasks.extend([health_task, sync_task])

                if ghather_logs:
                    logs_task = asyncio.create_task(self._fetch_logs(), name=f"fetch_logs_{id(self)}")
                    tasks.append(logs_task)

                await self.connect(
                    response.node_version,
                    response.core_version,
                    tasks,
                )
            except Exception as e:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise e

        return response

    async def stop(self, timeout: int = 10) -> None:
        """Stop the node with proper cleanup"""
        if await self.get_health() is Health.NOT_CONNECTED:
            return

        async with self._node_lock:
            await self.disconnect()

            try:
                await self._make_request(method="PUT", endpoint="stop", timeout=timeout)
            except Exception:
                pass

    async def info(self, timeout: int = 10) -> service.BaseInfoResponse | None:
        return await self._make_request(
            method="GET", endpoint="info", timeout=timeout, proto_response_class=service.BaseInfoResponse
        )

    async def get_system_stats(self, timeout: int = 10) -> service.SystemStatsResponse | None:
        return await self._make_request(
            method="GET", endpoint="stats/system", timeout=timeout, proto_response_class=service.SystemStatsResponse
        )

    async def get_backend_stats(self, timeout: int = 10) -> service.BackendStatsResponse | None:
        return await self._make_request(
            method="GET", endpoint="stats/backend", timeout=timeout, proto_response_class=service.BackendStatsResponse
        )

    async def get_stats(
        self, stat_type: service.StatType, reset: bool = True, name: str = "", timeout: int = 10
    ) -> service.StatResponse | None:
        return await self._make_request(
            method="GET",
            endpoint="stats",
            timeout=timeout,
            proto_message=service.StatRequest(reset=reset, name=name, type=stat_type),
            proto_response_class=service.StatResponse,
        )

    async def get_user_online_stats(self, email: str, timeout: int = 10) -> service.OnlineStatResponse | None:
        return await self._make_request(
            method="GET",
            endpoint="stats/user/online",
            timeout=timeout,
            proto_message=service.StatRequest(name=email),
            proto_response_class=service.OnlineStatResponse,
        )

    async def get_user_online_ip_list(self, email: str, timeout: int = 10) -> service.StatsOnlineIpListResponse | None:
        return await self._make_request(
            method="GET",
            endpoint="stats/user/online_ip",
            timeout=timeout,
            proto_message=service.StatRequest(name=email),
            proto_response_class=service.StatsOnlineIpListResponse,
        )

    async def sync_users(
        self, users: list[service.User], flush_queue: bool = False, timeout: int = 10
    ) -> service.Empty | None:
        if flush_queue:
            await self.flush_user_queue()

        async with self._node_lock:
            return await self._make_request(
                method="POST",
                endpoint="users/sync",
                timeout=timeout,
                proto_message=service.Users(users=users),
                proto_response_class=service.Empty,
            )

    async def _check_node_health(self):
        """Health check task with proper cancellation handling"""
        health_check_interval = 15
        consecutive_failures = 0
        max_failures = 3

        try:
            while not self.is_shutting_down():
                last_health = await self.get_health()

                if last_health in (Health.NOT_CONNECTED, Health.INVALID):
                    break

                try:
                    await asyncio.wait_for(self.get_backend_stats(), timeout=10)
                    consecutive_failures = 0
                    if last_health != Health.HEALTHY:
                        await self.set_health(Health.HEALTHY)
                except Exception:
                    consecutive_failures += 1
                    if consecutive_failures >= max_failures and last_health != Health.BROKEN:
                        await self.set_health(Health.BROKEN)

                try:
                    await asyncio.wait_for(asyncio.sleep(health_check_interval), timeout=health_check_interval + 1)
                except asyncio.TimeoutError:
                    continue

        except asyncio.CancelledError:
            pass
        except Exception:
            try:
                await self.set_health(Health.BROKEN)
            except Exception:
                pass

    async def _fetch_logs(self):
        """Log fetching task with proper cancellation handling"""
        retry_delay = 10
        max_retry_delay = 60

        try:
            while not self.is_shutting_down():
                health = await self.get_health()

                if health in (Health.NOT_CONNECTED, Health.INVALID):
                    break

                if health == Health.BROKEN:
                    try:
                        await asyncio.wait_for(asyncio.sleep(retry_delay), timeout=retry_delay + 1)
                    except asyncio.TimeoutError:
                        pass
                    retry_delay = min(retry_delay * 1.5, max_retry_delay)
                    continue

                retry_delay = 10

                try:
                    async with self._client.stream("GET", "/logs", timeout=60) as response:
                        buffer = b""

                        async for chunk in response.aiter_bytes():
                            if self.is_shutting_down():
                                break

                            buffer += chunk

                            while b"\n" in buffer:
                                line, buffer = buffer.split(b"\n", 1)
                                line = line.decode().strip()

                                if line:
                                    logs_queue = await self.get_logs()
                                    if logs_queue:
                                        await logs_queue.put(line)

                except asyncio.CancelledError:
                    break
                except Exception:
                    try:
                        await asyncio.wait_for(asyncio.sleep(retry_delay), timeout=retry_delay + 1)
                    except asyncio.TimeoutError:
                        pass

        except asyncio.CancelledError:
            pass

    async def _sync_user(self) -> None:
        """User sync task with proper cancellation handling"""
        retry_delay = 10
        max_retry_delay = 60

        try:
            while not self.is_shutting_down():
                health = await self.get_health()

                if health in (Health.NOT_CONNECTED, Health.INVALID):
                    break

                if health == Health.BROKEN:
                    try:
                        await asyncio.wait_for(asyncio.sleep(retry_delay), timeout=retry_delay + 1)
                    except asyncio.TimeoutError:
                        pass
                    retry_delay = min(retry_delay * 1.5, max_retry_delay)
                    continue

                retry_delay = 10
                user_task = None
                notify_task = None

                try:
                    async with self._lock.reader_lock:
                        if self._user_queue is None or self._notify_queue is None:
                            break

                        user_task = asyncio.create_task(self._user_queue.get())
                        notify_task = asyncio.create_task(self._notify_queue.get())

                    done, pending = await asyncio.wait(
                        [user_task, notify_task], return_when=asyncio.FIRST_COMPLETED, timeout=30
                    )

                    for task in pending:
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass

                    if not done:
                        continue

                    if notify_task in done:
                        notify_result = notify_task.result()
                        if notify_result is None:
                            break
                        continue

                    if user_task in done:
                        user = user_task.result()
                        if user is None:
                            break

                        try:
                            await self._make_request(
                                method="PUT",
                                endpoint="user/sync",
                                timeout=10,
                                proto_message=user,
                                proto_response_class=service.Empty,
                            )
                        except Exception:
                            try:
                                await asyncio.wait_for(asyncio.sleep(retry_delay), timeout=retry_delay + 1)
                            except asyncio.TimeoutError:
                                pass

                except asyncio.CancelledError:
                    if user_task and not user_task.done():
                        user_task.cancel()
                    if notify_task and not notify_task.done():
                        notify_task.cancel()
                    break
                except Exception:
                    try:
                        await asyncio.wait_for(asyncio.sleep(retry_delay), timeout=retry_delay + 1)
                    except asyncio.TimeoutError:
                        pass

        except asyncio.CancelledError:
            pass
