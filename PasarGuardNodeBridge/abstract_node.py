from abc import ABC, abstractmethod
from asyncio import Queue
from contextlib import AbstractAsyncContextManager

from PasarGuardNodeBridge.common import service_pb2 as service
from PasarGuardNodeBridge.controller import NodeAPIError
from PasarGuardNodeBridge.controller import Controller


class PasarGuardNode(Controller, ABC):
    @abstractmethod
    async def start(
        self,
        config: str,
        backend_type: service.BackendType,
        users: list[service.User],
        keep_alive: int = 0,
        exclude_inbounds: list[str] = [],
        timeout: int | None = None,
    ) -> service.BaseInfoResponse | None:
        raise NotImplementedError

    @abstractmethod
    async def stop(self, timeout: int | None = None) -> None:
        raise NotImplementedError

    @abstractmethod
    async def info(self, timeout: int | None = None) -> service.BaseInfoResponse | None:
        raise NotImplementedError

    @abstractmethod
    async def get_system_stats(self, timeout: int | None = None) -> service.SystemStatsResponse | None:
        raise NotImplementedError

    @abstractmethod
    async def get_backend_stats(self, timeout: int | None = None) -> service.BackendStatsResponse | None:
        raise NotImplementedError

    @abstractmethod
    async def get_stats(
        self, stat_type: service.StatType, reset: bool = True, name: str = "", timeout: int | None = None
    ) -> service.StatResponse | None:
        raise NotImplementedError

    @abstractmethod
    async def get_user_online_stats(self, email: str, timeout: int | None = None) -> service.OnlineStatResponse | None:
        raise NotImplementedError

    @abstractmethod
    async def get_user_online_ip_list(
        self, email: str, timeout: int | None = None
    ) -> service.StatsOnlineIpListResponse | None:
        raise NotImplementedError

    @abstractmethod
    async def sync_users(
        self, users: list[service.User], flush_pending: bool = False, timeout: int | None = None
    ) -> service.Empty | None:
        raise NotImplementedError

    @abstractmethod
    async def sync_users_chunked(
        self, users: list[service.User], chunk_size: int = 100, flush_pending: bool = False, timeout: int | None = None
    ) -> list[service.User]:
        raise NotImplementedError

    @abstractmethod
    async def _check_node_health(self):
        raise NotImplementedError

    @abstractmethod
    async def _sync_batch_users(self, users: list[service.User]) -> list[service.User]:
        """Sync a batch of users individually. Returns list of failed users to requeue."""
        raise NotImplementedError

    @abstractmethod
    def stream_logs(self, max_queue_size: int = 1000) -> AbstractAsyncContextManager[Queue[str | NodeAPIError]]:
        raise NotImplementedError
