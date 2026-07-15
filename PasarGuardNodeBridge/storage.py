import asyncio
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Protocol
from uuid import uuid4

from PasarGuardNodeBridge.common.service_pb2 import User


@dataclass(slots=True)
class NodeConfig:
    connection: str
    address: str
    port: int
    api_port: int
    server_ca: str
    api_key: str
    name: str = "default"
    extra: dict[str, Any] = field(default_factory=dict)
    default_timeout: int = 10
    internal_timeout: int = 15
    proxy: str | None = None
    max_message_size: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NodeConfig":
        return cls(**data)


@dataclass(slots=True)
class ClaimedUser:
    token: str
    user: User


class NodeRegistryProtocol(Protocol):
    async def upsert_node(self, node_id: str, config: NodeConfig) -> None: ...
    async def get_node(self, node_id: str) -> NodeConfig | None: ...
    async def delete_node(self, node_id: str) -> None: ...
    async def list_nodes(self) -> list[str]: ...


class UserSyncStoreProtocol(Protocol):
    async def enqueue_users(self, node_id: str, users: list[User]) -> None: ...

    async def claim_users(
        self, node_id: str, worker_id: str, limit: int, lease_seconds: float
    ) -> list[ClaimedUser]: ...

    async def ack_users(self, node_id: str, tokens: list[str]) -> None: ...
    async def requeue_users(self, node_id: str, claimed_users: list[ClaimedUser]) -> None: ...
    async def clear(self, node_id: str) -> None: ...


class LifecycleOperation(str, Enum):
    START = "start"
    STOP = "stop"
    RECONNECT = "reconnect"
    UPDATE_NODE = "update_node"
    UPDATE_CORE = "update_core"
    UPDATE_GEOFILES = "update_geofiles"
    HARD_RESET = "hard_reset"


class LifecycleStatus(str, Enum):
    UNKNOWN = "unknown"
    STARTING = "starting"
    HEALTHY = "healthy"
    STOPPING = "stopping"
    STOPPED = "stopped"
    BROKEN = "broken"


@dataclass(slots=True)
class LifecycleLease:
    node_id: str
    worker_id: str
    operation: LifecycleOperation
    token: str
    epoch: int
    lease_seconds: float = 30.0


@dataclass(slots=True)
class NodeLifecycleState:
    desired: LifecycleStatus = LifecycleStatus.UNKNOWN
    observed: LifecycleStatus = LifecycleStatus.UNKNOWN
    epoch: int = 0
    operation: LifecycleOperation | None = None
    owner: str | None = None
    node_version: str = ""
    core_version: str = ""
    updated_at: float = 0.0


class NodeLifecycleCoordinatorProtocol(Protocol):
    async def try_acquire(
        self, node_id: str, worker_id: str, operation: LifecycleOperation, lease_seconds: float
    ) -> LifecycleLease | None: ...

    async def release(self, lease: LifecycleLease, state_update: NodeLifecycleState | None = None) -> None: ...
    async def heartbeat(self, lease: LifecycleLease) -> None: ...
    async def get_state(self, node_id: str) -> NodeLifecycleState | None: ...

    async def update_observed(
        self, node_id: str, observed: LifecycleStatus, expected_epoch: int | None = None
    ) -> None: ...


class InMemoryNodeRegistry:
    def __init__(self):
        self._nodes: dict[str, NodeConfig] = {}
        self._lock = asyncio.Lock()

    async def upsert_node(self, node_id: str, config: NodeConfig) -> None:
        async with self._lock:
            self._nodes[node_id] = config

    async def get_node(self, node_id: str) -> NodeConfig | None:
        async with self._lock:
            return self._nodes.get(node_id)

    async def delete_node(self, node_id: str) -> None:
        async with self._lock:
            self._nodes.pop(node_id, None)

    async def list_nodes(self) -> list[str]:
        async with self._lock:
            return list(self._nodes)


class InMemoryUserSyncStore:
    def __init__(self):
        self._pending: dict[str, dict[str, User]] = {}
        self._claimed: dict[str, dict[str, tuple[User, float]]] = {}
        self._lock = asyncio.Lock()

    async def enqueue_users(self, node_id: str, users: list[User]) -> None:
        if not users:
            return
        async with self._lock:
            pending = self._pending.setdefault(node_id, {})
            for user in users:
                pending[user.email] = user

    async def claim_users(self, node_id: str, worker_id: str, limit: int, lease_seconds: float) -> list[ClaimedUser]:
        if limit <= 0:
            return []
        now = time.monotonic()
        async with self._lock:
            pending = self._pending.setdefault(node_id, {})
            claimed = self._claimed.setdefault(node_id, {})

            for token, (user, expires_at) in list(claimed.items()):
                if expires_at <= now:
                    pending.setdefault(user.email, user)
                    del claimed[token]

            result: list[ClaimedUser] = []
            for email, user in list(pending.items()):
                token = f"{worker_id}:{uuid4()}"
                claimed[token] = (user, now + lease_seconds)
                result.append(ClaimedUser(token=token, user=user))
                del pending[email]
                if len(result) >= limit:
                    break
            return result

    async def ack_users(self, node_id: str, tokens: list[str]) -> None:
        if not tokens:
            return
        async with self._lock:
            claimed = self._claimed.setdefault(node_id, {})
            for token in tokens:
                claimed.pop(token, None)

    async def requeue_users(self, node_id: str, claimed_users: list[ClaimedUser]) -> None:
        if not claimed_users:
            return
        async with self._lock:
            pending = self._pending.setdefault(node_id, {})
            claimed = self._claimed.setdefault(node_id, {})
            for item in claimed_users:
                claimed.pop(item.token, None)
                pending.setdefault(item.user.email, item.user)

    async def clear(self, node_id: str) -> None:
        async with self._lock:
            self._pending.pop(node_id, None)
            self._claimed.pop(node_id, None)


class InMemoryNodeLifecycleCoordinator:
    def __init__(self):
        self._states: dict[str, NodeLifecycleState] = {}
        self._leases: dict[str, tuple[LifecycleLease, float]] = {}
        self._lock = asyncio.Lock()

    async def try_acquire(
        self, node_id: str, worker_id: str, operation: LifecycleOperation, lease_seconds: float
    ) -> LifecycleLease | None:
        now = time.monotonic()
        async with self._lock:
            current = self._leases.get(node_id)
            if current is not None:
                _, expires_at = current
                if expires_at > now:
                    return None
                self._leases.pop(node_id, None)

            state = self._states.get(node_id) or NodeLifecycleState(updated_at=now)
            epoch = state.epoch + 1
            lease = LifecycleLease(
                node_id=node_id,
                worker_id=worker_id,
                operation=operation,
                token=f"{worker_id}:{uuid4()}",
                epoch=epoch,
                lease_seconds=lease_seconds,
            )
            state.epoch = epoch
            state.operation = operation
            state.owner = worker_id
            state.updated_at = now
            if operation is LifecycleOperation.START:
                state.desired = LifecycleStatus.HEALTHY
                state.observed = LifecycleStatus.STARTING
            elif operation is LifecycleOperation.STOP:
                state.desired = LifecycleStatus.STOPPED
                state.observed = LifecycleStatus.STOPPING
            self._states[node_id] = state
            self._leases[node_id] = (lease, now + lease_seconds)
            return lease

    async def release(self, lease: LifecycleLease, state_update: NodeLifecycleState | None = None) -> None:
        now = time.monotonic()
        async with self._lock:
            current = self._leases.get(lease.node_id)
            if current is None or current[0].token != lease.token:
                return
            self._leases.pop(lease.node_id, None)
            state = state_update or self._states.get(lease.node_id) or NodeLifecycleState()
            if state.epoch != lease.epoch:
                state.epoch = lease.epoch
            state.operation = None
            state.owner = None
            state.updated_at = now
            self._states[lease.node_id] = state

    async def heartbeat(self, lease: LifecycleLease) -> None:
        now = time.monotonic()
        async with self._lock:
            current = self._leases.get(lease.node_id)
            if current is not None and current[0].token == lease.token:
                self._leases[lease.node_id] = (lease, now + lease.lease_seconds)

    async def get_state(self, node_id: str) -> NodeLifecycleState | None:
        async with self._lock:
            return self._states.get(node_id)

    async def update_observed(self, node_id: str, observed: LifecycleStatus, expected_epoch: int | None = None) -> None:
        now = time.monotonic()
        async with self._lock:
            state = self._states.get(node_id) or NodeLifecycleState(updated_at=now)
            if expected_epoch is not None and state.epoch != expected_epoch:
                return
            state.observed = observed
            state.updated_at = now
            self._states[node_id] = state


_default_user_sync_store = InMemoryUserSyncStore()
_default_lifecycle_coordinator = InMemoryNodeLifecycleCoordinator()


def get_default_user_sync_store() -> InMemoryUserSyncStore:
    """Return the process-local store shared by default controller instances."""
    return _default_user_sync_store


def get_default_lifecycle_coordinator() -> InMemoryNodeLifecycleCoordinator:
    """Return the process-local coordinator shared by default controller instances."""
    return _default_lifecycle_coordinator
