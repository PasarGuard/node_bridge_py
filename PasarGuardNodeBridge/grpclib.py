import asyncio

from grpclib.client import Channel
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
        extra: dict | None = None,
        max_logs: int = 1000,
    ):
        super().__init__(server_ca, api_key, extra, max_logs)

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

    async def _handle_error(self, error: Exception):
        """Convert gRPC errors to NodeAPIError with HTTP status codes."""
        if isinstance(error, asyncio.TimeoutError):
            raise NodeAPIError(-1, "Request timed out")
        elif isinstance(error, GRPCError):
            http_status = grpc_to_http_status(error.status)
            raise NodeAPIError(http_status, error.message)
        elif isinstance(error, StreamTerminatedError):
            raise NodeAPIError(-1, f"Stream terminated: {str(error)}")
        else:
            raise NodeAPIError(0, str(error))

    async def _handle_grpc_request(self, method, request, timeout=15):
        """Handle a gRPC request and convert errors to NodeAPIError."""
        try:
            return await asyncio.wait_for(method(request, metadata=self._metadata), timeout=timeout)
        except Exception as e:
            await self._handle_error(e)

    async def start(
        self,
        config: str,
        backend_type: service.BackendType,
        users: list[service.User],
        keep_alive: int = 0,
        ghather_logs: bool = True,
        timeout: int = 15,
    ) -> service.BaseInfoResponse | None:
        """Start the node with proper task management"""
        health = await self.get_health()
        if health in (Health.BROKEN, Health.HEALTHY):
            await self.stop()
        elif health is Health.INVALID:
            raise NodeAPIError(code=-4, detail="Invalid node")

        req = service.Backend(type=backend_type, config=config, users=users, keep_alive=keep_alive)

        async with self._node_lock:
            info = await self._handle_grpc_request(
                method=self._client.Start,
                request=req,
                timeout=timeout,
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
                    info.node_version,
                    info.core_version,
                    tasks,
                )
            except Exception as e:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise e

            return info

    async def stop(self, timeout: int = 10) -> None:
        """Stop the node with proper cleanup"""
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

    async def info(self, timeout: int = 10) -> service.BaseInfoResponse | None:
        return await self._handle_grpc_request(
            method=self._client.GetBaseInfo,
            request=service.Empty(),
            timeout=timeout,
        )

    async def get_system_stats(self, timeout: int = 10) -> service.SystemStatsResponse | None:
        return await self._handle_grpc_request(
            method=self._client.GetSystemStats,
            request=service.Empty(),
            timeout=timeout,
        )

    async def get_backend_stats(self, timeout: int = 10) -> service.BackendStatsResponse | None:
        return await self._handle_grpc_request(
            method=self._client.GetBackendStats,
            request=service.Empty(),
            timeout=timeout,
        )

    async def get_stats(
        self, stat_type: service.StatType, reset: bool = True, name: str = "", timeout: int = 10
    ) -> service.StatResponse | None:
        return await self._handle_grpc_request(
            method=self._client.GetStats,
            request=service.StatRequest(reset=reset, name=name, type=stat_type),
            timeout=timeout,
        )

    async def get_user_online_stats(self, email: str, timeout: int = 10) -> service.OnlineStatResponse | None:
        return await self._handle_grpc_request(
            method=self._client.GetUserOnlineStats,
            request=service.StatRequest(name=email),
            timeout=timeout,
        )

    async def get_user_online_ip_list(self, email: str, timeout: int = 10) -> service.StatsOnlineIpListResponse | None:
        return await self._handle_grpc_request(
            method=self._client.GetUserOnlineIpListStats,
            request=service.StatRequest(name=email),
            timeout=timeout,
        )

    async def sync_users(
        self, users: list[service.User], flush_queue: bool = False, timeout: int = 10
    ) -> service.Empty | None:
        if flush_queue:
            await self.flush_user_queue()

        async with self._node_lock:
            return await self._handle_grpc_request(
                method=self._client.SyncUsers,
                request=service.Users(users=users),
                timeout=timeout,
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
                    async with self._client.GetLogs.open(metadata=self._metadata) as stream:
                        await stream.send_message(service.Empty())

                        while not self.is_shutting_down():
                            try:
                                log = await asyncio.wait_for(stream.recv_message(), timeout=30)
                                if log is None:
                                    continue

                                logs_queue = await self.get_logs()
                                if logs_queue:
                                    await logs_queue.put(log.detail)

                            except asyncio.TimeoutError:
                                continue
                            except Exception:
                                break

                except asyncio.CancelledError:
                    break
                except Exception:
                    try:
                        await asyncio.wait_for(asyncio.sleep(retry_delay), timeout=retry_delay + 1)
                    except asyncio.TimeoutError:
                        pass

        except asyncio.CancelledError:
            pass

    async def _sync_user(self):
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

                try:
                    async with self._client.SyncUser.open(metadata=self._metadata) as stream:
                        while not self.is_shutting_down():
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
                                        await asyncio.wait_for(stream.send_message(user), timeout=10)
                                    except Exception:
                                        break

                            except asyncio.CancelledError:
                                if user_task and not user_task.done():
                                    user_task.cancel()
                                if notify_task and not notify_task.done():
                                    notify_task.cancel()
                                break
                            except Exception:
                                break

                except asyncio.CancelledError:
                    break
                except Exception:
                    try:
                        await asyncio.wait_for(asyncio.sleep(retry_delay), timeout=retry_delay + 1)
                    except asyncio.TimeoutError:
                        pass

        except asyncio.CancelledError:
            pass
