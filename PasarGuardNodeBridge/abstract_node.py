from abc import ABC, abstractmethod

from PasarGuardNodeBridge.common import service_pb2 as service
from PasarGuardNodeBridge.controller import Controller


class PasarGuardNode(Controller, ABC):
    @abstractmethod
    async def start(
        self,
        config: str,
        backend_type: service.BackendType,
        users: list[service.User],
        keep_alive: int,
        ghather_logs: bool,
        timeout: int,
    ) -> service.BaseInfoResponse | None:
        raise NotImplementedError

    @abstractmethod
    async def stop(self, timeout: int) -> None:
        raise NotImplementedError

    @abstractmethod
    async def info(self, timeout: int) -> service.BaseInfoResponse | None:
        raise NotImplementedError

    @abstractmethod
    async def get_system_stats(self, timeout: int) -> service.SystemStatsResponse | None:
        raise NotImplementedError

    @abstractmethod
    async def get_backend_stats(self, timeout: int) -> service.BackendStatsResponse | None:
        raise NotImplementedError

    @abstractmethod
    async def get_stats(
        self, stat_type: service.StatType, reset: bool = True, name: str = "", timeout: int = 10
    ) -> service.StatResponse | None:
        raise NotImplementedError

    @abstractmethod
    async def get_user_online_stats(self, email: str, timeout: int) -> service.OnlineStatResponse | None:
        raise NotImplementedError

    @abstractmethod
    async def get_user_online_ip_list(self, email: str, timeout: int) -> service.StatsOnlineIpListResponse | None:
        raise NotImplementedError

    @abstractmethod
    async def sync_users(self, users: list[service.User], flush_queue: bool, timeout: int) -> service.Empty | None:
        raise NotImplementedError

    @abstractmethod
    async def _check_node_health(self):
        raise NotImplementedError

    @abstractmethod
    async def _fetch_logs(self):
        raise NotImplementedError

    @abstractmethod
    async def _sync_user(self):
        raise NotImplementedError
