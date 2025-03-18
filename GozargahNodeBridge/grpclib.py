import asyncio
from http import HTTPStatus

from grpclib.client import Channel
from grpclib.config import Configuration
from grpclib.exceptions import GRPCError, StreamTerminatedError
from aiorwlock import RWLock

from GozargahNodeBridge.common import service_pb2 as service
from GozargahNodeBridge.common import service_grpc
from GozargahNodeBridge.controller import NodeAPIError, Health
from GozargahNodeBridge.abstract_node import GozargahNode
from GozargahNodeBridge.utils import grpc_to_http_status


class Node(GozargahNode):
    def __init__(
        self,
        address: str,
        port: int,
        client_cert: str,
        client_key: str,
        server_ca: str,
        extra: dict | None = None,
        max_logs: int = 1000,
    ):
        super().__init__(client_cert, client_key, server_ca, extra, max_logs)
        try:
            self.channel = Channel(host=address, port=port, ssl=self.ctx, config=Configuration(_keepalive_timeout=10))
            self._client = service_grpc.NodeServiceStub(self.channel)
        except Exception as e:
            self._cleanup_temp_files()
            raise NodeAPIError(-1, f"Channel initialization failed: {str(e)}")

        self._metadata = None
        self._node_lock = RWLock()

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
        self._cleanup_temp_files()
        self._close_chan()

    def __del__(self):
        self._cleanup_temp_files()
        self._close_chan()

    async def _handle_error(self, error: Exception):
        """Convert gRPC errors to NodeAPIError with HTTP status codes."""
        if isinstance(error, asyncio.TimeoutError):
            raise NodeAPIError(-1, "Request timed out")
        elif isinstance(error, GRPCError):
            grpc_status = error.status
            http_status = grpc_to_http_status(grpc_status)
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
        timeout: int = 15,
    ) -> service.BaseInfoResponse | None:
        """Start the node"""
        health = await self.get_health()
        if health in (Health.BROKEN, Health.HEALTHY):
            await self.stop()
        elif health is Health.INVALID:
            raise NodeAPIError(code=-4, detail="Invalid node")

        req = service.Backend(type=backend_type, config=config, users=users, keep_alive=keep_alive)

        async with self._node_lock.writer_lock:
            info = await self._handle_grpc_request(
                method=self._client.Start,
                request=req,
                timeout=timeout,
            )
            await self.connect(
                info.node_version,
                info.core_version,
                [
                    asyncio.create_task(self._check_node_health()),
                    asyncio.create_task(self._sync_user()),
                    asyncio.create_task(self._fetch_logs()),
                ],
            )
            self._metadata = {"authorization": f"Bearer {info.session_id}"}
            return info

    async def stop(self, timeout: int = 10) -> None:
        """Stop the node"""
        if await self.get_health() is Health.NOT_CONNECTED:
            return

        async with self._node_lock.writer_lock:
            await self.disconnect()

            await self._handle_grpc_request(
                method=self._client.Stop,
                request=service.Empty(),
                timeout=timeout,
            )

            self._metadata = None

    async def info(self, timeout: int = 10) -> service.BaseInfoResponse | None:
        await self.connected()

        async with self._node_lock.reader_lock:
            return await self._handle_grpc_request(
                method=self._client.GetBaseInfo,
                request=service.Empty(),
                timeout=timeout,
            )

    async def get_system_stats(self, timeout: int = 10) -> service.SystemStatsResponse | None:
        await self.connected()

        async with self._node_lock.reader_lock:
            return await self._handle_grpc_request(
                method=self._client.GetSystemStats,
                request=service.Empty(),
                timeout=timeout,
            )

    async def get_backend_stats(self, timeout: int = 10) -> service.BackendStatsResponse | None:
        await self.connected()

        async with self._node_lock.reader_lock:
            return await self._handle_grpc_request(
                method=self._client.GetBackendStats,
                request=service.Empty(),
                timeout=timeout,
            )

    async def get_outbounds_stats(self, reset: bool = True, timeout: int = 10) -> service.StatResponse | None:
        await self.connected()

        async with self._node_lock.reader_lock:
            return await self._handle_grpc_request(
                method=self._client.GetOutboundsStats,
                request=service.StatRequest(reset=reset),
                timeout=timeout,
            )

    async def get_outbound_stats(self, tag: str, reset: bool = True, timeout: int = 10) -> service.StatResponse | None:
        await self.connected()

        async with self._node_lock.reader_lock:
            return await self._handle_grpc_request(
                method=self._client.GetOutboundStats,
                request=service.StatRequest(name=tag, reset=reset),
                timeout=timeout,
            )

    async def get_inbounds_stats(self, reset: bool = True, timeout: int = 10) -> service.StatResponse | None:
        await self.connected()

        async with self._node_lock.reader_lock:
            return await self._handle_grpc_request(
                method=self._client.GetInboundsStats,
                request=service.StatRequest(reset=reset),
                timeout=timeout,
            )

    async def get_inbound_stats(self, tag: str, reset: bool = True, timeout: int = 10) -> service.StatResponse | None:
        await self.connected()

        async with self._node_lock.reader_lock:
            return await self._handle_grpc_request(
                method=self._client.GetInboundStats,
                request=service.StatRequest(name=tag, reset=reset),
                timeout=timeout,
            )

    async def get_users_stats(self, reset: bool = True, timeout: int = 10) -> service.StatResponse | None:
        await self.connected()

        async with self._node_lock.reader_lock:
            return await self._handle_grpc_request(
                method=self._client.GetUsersStats,
                request=service.StatRequest(reset=reset),
                timeout=timeout,
            )

    async def get_user_stats(self, email: str, reset: bool = True, timeout: int = 10) -> service.StatResponse | None:
        await self.connected()

        async with self._node_lock.reader_lock:
            return await self._handle_grpc_request(
                method=self._client.GetUserStats,
                request=service.StatRequest(name=email, reset=reset),
                timeout=timeout,
            )

    async def get_user_online_stats(self, email: str, timeout: int = 10) -> service.OnlineStatResponse | None:
        await self.connected()

        async with self._node_lock.reader_lock:
            return await self._handle_grpc_request(
                method=self._client.GetUserOnlineStats,
                request=service.StatRequest(name=email),
                timeout=timeout,
            )
    
    async def get_user_online_ip_list(self, email: str, timeout: int = 10) -> service.StatsOnlineIpListResponse | None:
        await self.connected()

        async with self._node_lock.reader_lock:
            return await self._handle_grpc_request(
                method=self._client.GetUserOnlineIpListStats,
                request=service.StatRequest(name=email),
                timeout=timeout,
            )

    async def sync_users(
        self, users: list[service.User], flush_queue: bool = False, timeout: int = 10
    ) -> service.Empty | None:
        await self.connected()
        if flush_queue:
            self.flush_user_queue()

        async with self._node_lock.writer_lock:
            return await self._handle_grpc_request(
                method=self._client.SyncUsers,
                request=service.Users(users=users),
                timeout=timeout,
            )

    async def _check_node_health(self):
        while True:
            last_health = await self.get_health()

            try:
                await self.get_backend_stats()
                if last_health != Health.HEALTHY:
                    await self.set_health(Health.HEALTHY)
            except Exception:
                if last_health != Health.BROKEN:
                    await self.set_health(Health.BROKEN)

            await asyncio.sleep(2)

    async def _fetch_logs(self):
        while True:
            health = await self.get_health()
            if health == Health.BROKEN:
                await asyncio.sleep(5)
                continue
            elif health == Health.NOT_CONNECTED:
                return

            try:
                async with self._client.GetLogs.open(metadata=self._metadata) as stream:
                    await stream.send_message(service.Empty())
                    while True:
                        log = await stream.recv_message()
                        if log is None:
                            continue
                        await self._logs_queue.put(log.detail)

            except Exception:
                await asyncio.sleep(3)
                continue

    async def _sync_user(self):
        while True:
            health = await self.get_health()

            if health == Health.BROKEN:
                await asyncio.sleep(5)
                continue
            elif health == Health.NOT_CONNECTED:
                return

            async with self._client.SyncUser.open(metadata=self._metadata) as stream:
                async with self._lock.reader_lock:
                    if self._user_queue is None or self._notify_queue is None:
                        return

                    user_task = asyncio.create_task(self._user_queue.get())
                    notify_task = asyncio.create_task(self._notify_queue.get())

                try:
                    # Wait for first completed operation
                    done, pending = await asyncio.wait([user_task, notify_task], return_when=asyncio.FIRST_COMPLETED)

                except asyncio.CancelledError:
                    # Cleanup if task is cancelled
                    user_task.cancel()
                    notify_task.cancel()
                    raise

                for task in pending:
                    task.cancel()

                if notify_task in done:
                    continue

                # Handle UserChan message
                if user_task in done:
                    user = user_task.result()
                    if user is None:
                        continue

                    try:
                        # Send the user through the gRPC stream
                        await stream.send_message(user)
                    except Exception:
                        await asyncio.sleep(5)
                        continue
