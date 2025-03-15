from abc import ABC, abstractmethod

from GozargahNodeBridge.common import service_pb2 as service
from GozargahNodeBridge.controller import Controller


class GozargahNode(Controller, ABC):
    @abstractmethod
    async def start(
        self, config: str, backend_type: service.BackendType, users: list[service.User], keep_alive: int, timeout: int
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
    async def get_outbounds_stats(self, reset: bool, timeout: int) -> service.StatResponse | None:
        raise NotImplementedError

    @abstractmethod
    async def get_outbound_stats(self, tag: str, reset: bool, timeout: int) -> service.StatResponse | None:
        raise NotImplementedError

    @abstractmethod
    async def get_inbounds_stats(self, reset: bool, timeout: int) -> service.StatResponse | None:
        raise NotImplementedError

    @abstractmethod
    async def get_inbound_stats(self, tag: str, reset: bool, timeout: int) -> service.StatResponse | None:
        raise NotImplementedError

    @abstractmethod
    async def get_users_stats(self, reset: bool, timeout: int) -> service.StatResponse | None:
        raise NotImplementedError

    @abstractmethod
    async def get_user_stats(self, email: str, reset: bool, timeout: int) -> service.StatResponse | None:
        raise NotImplementedError

    @abstractmethod
    async def get_user_online_stats(self, email: str, timeout: int) -> service.OnlineStatResponse | None:
        raise NotImplementedError
    
    @abstractmethod
    async def get_user_online_ip_list(self, email: str, timeout: int) -> service.StatsOnlineIpListResponse | None:
        raise NotImplementedError

    @abstractmethod
    async def sync_users(self, users: list[service.User], timeout: int) -> service.Empty | None:
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
