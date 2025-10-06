import asyncio
import logging

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
        max_logs: int = 1000,
        logger: logging.Logger | None = None,
    ):
        super().__init__(server_ca, api_key, name, extra, max_logs, logger)

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
            message = error.message or "Unknown gRPC error"
            raise NodeAPIError(http_status, message)
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
        exclude_inbounds: list[str] = [],
        timeout: int = 10,
    ) -> service.BaseInfoResponse | None:
        """Start the node with proper task management"""
        health = await self.get_health()
        if health in (Health.BROKEN, Health.HEALTHY):
            await self.stop()
        elif health is Health.INVALID:
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

            # Clear shutdown event before creating tasks
            self._shutdown_event.clear()

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

    async def _sync_user_with_retry(
        self, stream: Stream[service.User, service.Empty], user: service.User, max_retries: int = 3, timeout: int = 10
    ) -> bool:
        """
        Attempt to sync a user via gRPC stream with retry logic for timeout errors.
        Returns True if successful, False if all retries failed.
        """
        for attempt in range(max_retries):
            try:
                await asyncio.wait_for(stream.send_message(user), timeout=timeout)
                return True
            except asyncio.TimeoutError:
                # Retry on timeout
                if attempt < max_retries - 1:
                    # Short delay before retry
                    await asyncio.sleep(0.5)
                    continue
                # Last attempt failed
                return False
            except Exception:
                # Other errors, don't retry
                return False
        return False

    async def _check_node_health(self):
        """Health check task with proper cancellation handling"""
        health_check_interval = 10
        max_retries = 3
        retry_delay = 2
        retries = 0
        self.logger.info(f"[{self.name}] Health check task started")

        try:
            while not self.is_shutting_down():
                last_health = await self.get_health()

                if last_health in (Health.NOT_CONNECTED, Health.INVALID):
                    self.logger.info(f"[{self.name}] Health check task stopped due to node state")
                    return

                try:
                    await asyncio.wait_for(self.get_backend_stats(), timeout=10)
                    if last_health != Health.HEALTHY:
                        self.logger.info(f"[{self.name}] Node health is HEALTHY")
                        await self.set_health(Health.HEALTHY)
                    retries = 0
                except Exception as e:
                    retries += 1
                    if retries >= max_retries:
                        if last_health != Health.BROKEN:
                            self.logger.error(
                                f"[{self.name}] Health check failed after {max_retries} retries: {e}, setting health to BROKEN"
                            )
                            await self.set_health(Health.BROKEN)
                    else:
                        self.logger.warning(f"[{self.name}] Health check failed, retry {retries}/{max_retries} in {retry_delay}s: {e}")
                        await asyncio.sleep(retry_delay)
                        continue

                try:
                    await asyncio.wait_for(asyncio.sleep(health_check_interval), timeout=health_check_interval + 1)
                except asyncio.TimeoutError:
                    continue

        except asyncio.CancelledError:
            self.logger.info(f"[{self.name}] Health check task cancelled")
        except Exception as e:
            self.logger.error(f"[{self.name}] An unexpected error occurred in health check task: {e}")
            try:
                await self.set_health(Health.BROKEN)
            except Exception as e_set_health:
                self.logger.error(
                    f"[{self.name}] Failed to set health to BROKEN after unexpected error: {e_set_health}"
                )
        finally:
            self.logger.info(f"[{self.name}] Health check task finished")

    async def _fetch_logs(self):
        """Log fetching task with proper cancellation handling"""
        retry_delay = 10
        max_retry_delay = 60
        log_retry_delay = 1
        self.logger.info(f"[{self.name}] Log fetching task started")

        try:
            while not self.is_shutting_down():
                health = await self.get_health()

                if health in (Health.NOT_CONNECTED, Health.INVALID):
                    self.logger.info(f"[{self.name}] Log fetching task stopped due to node state")
                    break

                if health == Health.BROKEN:
                    self.logger.warning(
                        f"[{self.name}] Node is broken, waiting for {retry_delay} seconds before refetching logs"
                    )
                    try:
                        await asyncio.wait_for(asyncio.sleep(retry_delay), timeout=retry_delay + 1)
                    except asyncio.TimeoutError:
                        pass
                    retry_delay = min(retry_delay * 1.5, max_retry_delay)
                    continue

                retry_delay = 10

                stream_failed = False
                try:
                    self.logger.info(f"[{self.name}] Opening log stream")
                    async with self._client.GetLogs.open(metadata=self._metadata) as stream:
                        await stream.send_message(service.Empty())
                        self.logger.info(f"[{self.name}] Log stream opened successfully")
                        log_retry_delay = 1

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
                                stream_failed = True
                                break

                except asyncio.CancelledError:
                    self.logger.info(f"[{self.name}] Log fetching task cancelled during stream")
                    break
                except Exception as e:
                    self.logger.error(f"[{self.name}] Log stream failed: {e}")
                    stream_failed = True

                if stream_failed:
                    self.logger.warning(f"[{self.name}] Log stream failed, retrying in {log_retry_delay} seconds")
                    try:
                        await asyncio.wait_for(asyncio.sleep(log_retry_delay), timeout=log_retry_delay + 1)
                    except asyncio.TimeoutError:
                        pass
                    log_retry_delay = min(log_retry_delay * 2, max_retry_delay)

        except asyncio.CancelledError:
            self.logger.info(f"[{self.name}] Log fetching task cancelled")
        finally:
            self.logger.info(f"[{self.name}] Log fetching task finished")

    async def _sync_user(self):
        """User sync task with proper cancellation handling"""
        retry_delay = 10
        max_retry_delay = 60
        sync_retry_delay = 1
        self.logger.info(f"[{self.name}] User sync task started")

        try:
            while not self.is_shutting_down():
                health = await self.get_health()

                if health in (Health.NOT_CONNECTED, Health.INVALID):
                    self.logger.info(f"[{self.name}] User sync task stopped due to node state")
                    break

                if health == Health.BROKEN:
                    self.logger.warning(
                        f"[{self.name}] Node is broken, waiting for {retry_delay} seconds before syncing users"
                    )
                    try:
                        await asyncio.wait_for(asyncio.sleep(retry_delay), timeout=retry_delay + 1)
                    except asyncio.TimeoutError:
                        pass
                    retry_delay = min(retry_delay * 1.5, max_retry_delay)
                    continue

                retry_delay = 10

                stream_failed = False
                try:
                    self.logger.info(f"[{self.name}] Opening user sync stream")
                    async with self._client.SyncUser.open(metadata=self._metadata) as stream:
                        self.logger.info(f"[{self.name}] User sync stream opened successfully")
                        sync_retry_delay = 1
                        while not self.is_shutting_down():
                            user_task = None
                            notify_task = None

                            try:
                                async with self._lock.reader_lock:
                                    if self._user_queue is None or self._notify_queue is None:
                                        stream_failed = True
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
                                        self.logger.info(f"[{self.name}] Received notification to stop user sync task")
                                        stream_failed = True
                                        break
                                    continue

                                if user_task in done:
                                    user = user_task.result()
                                    if user is None:
                                        self.logger.info(f"[{self.name}] Received None user, stopping user sync task")
                                        stream_failed = True
                                        break

                                    self.logger.info(f"[{self.name}] Syncing user {user.email}")
                                    success = await self._sync_user_with_retry(stream, user, max_retries=3, timeout=15)
                                    if success:
                                        self.logger.info(f"[{self.name}] Successfully synced user {user.email}")
                                        sync_retry_delay = 1
                                    else:
                                        self.logger.warning(
                                            f"[{self.name}] Failed to sync user {user.email}, requeueing"
                                        )
                                        await self.requeue_user_with_deduplication(user)
                                        try:
                                            await asyncio.wait_for(asyncio.sleep(sync_retry_delay), timeout=sync_retry_delay + 1)
                                        except asyncio.TimeoutError:
                                            pass
                                        sync_retry_delay = min(sync_retry_delay * 2, max_retry_delay)

                            except asyncio.CancelledError:
                                self.logger.info(f"[{self.name}] User sync task cancelled")
                                if user_task and not user_task.done():
                                    user_task.cancel()
                                if notify_task and not notify_task.done():
                                    notify_task.cancel()
                                stream_failed = True
                                break
                            except Exception as e:
                                self.logger.error(f"[{self.name}] An error occurred in user sync task: {e}")
                                try:
                                    await asyncio.wait_for(asyncio.sleep(sync_retry_delay), timeout=sync_retry_delay + 1)
                                except asyncio.TimeoutError:
                                    pass
                                sync_retry_delay = min(sync_retry_delay * 2, max_retry_delay)
                        if stream_failed:
                            break

                except asyncio.CancelledError:
                    self.logger.info(f"[{self.name}] User sync stream cancelled")
                    break
                except Exception as e:
                    self.logger.error(f"[{self.name}] User sync stream failed: {e}")
                    stream_failed = True

                if stream_failed:
                    self.logger.warning(
                        f"[{self.name}] User sync stream failed, retrying in {sync_retry_delay} seconds"
                    )
                    try:
                        await asyncio.wait_for(asyncio.sleep(sync_retry_delay), timeout=sync_retry_delay + 1)
                    except asyncio.TimeoutError:
                        pass
                    sync_retry_delay = min(sync_retry_delay * 2, max_retry_delay)

        except asyncio.CancelledError:
            self.logger.info(f"[{self.name}] User sync task cancelled")
        finally:
            self.logger.info(f"[{self.name}] User sync task finished")
