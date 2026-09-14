import asyncio
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from itertools import islice
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

    async def ack_users(self, node_id: str, tokens: list[str]) -> list[str] | None: ...
    async def requeue_users(self, node_id: str, claimed_users: list[ClaimedUser]) -> None: ...
    async def clear(self, node_id: str) -> None: ...


class SnapshotUserSyncStoreProtocol(UserSyncStoreProtocol, Protocol):
    """Optional shared coordination for full snapshots; all operations must be atomic."""

    async def begin_full_sync(self, node_id: str, worker_id: str, lease_seconds: float) -> str | None: ...
    async def renew_full_sync(self, node_id: str, token: str, lease_seconds: float) -> bool: ...
    async def end_full_sync(self, node_id: str, token: str) -> None: ...
    async def fence_state(self, node_id: str) -> tuple[bool, int]: ...
    async def capture_queued(self, node_id: str) -> tuple[dict[str, int], int]: ...
    async def retire_captured(self, node_id: str, captured: dict[str, int]) -> int: ...


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
        # Keep the latest desired revision even while an older revision is leased.
        self._pending: dict[str, dict[str, tuple[User, int]]] = {}
        self._claimed: dict[str, dict[str, tuple[str, int, float]]] = {}
        self._active: dict[str, dict[str, str]] = {}
        self._revision = 0
        self._fences: dict[str, tuple[str, float, int]] = {}
        self._lock = asyncio.Lock()

    async def enqueue_users(self, node_id: str, users: list[User]) -> None:
        if not users:
            return
        async with self._lock:
            pending = self._pending.setdefault(node_id, {})
            for user in users:
                self._revision += 1
                pending[user.email] = (User.FromString(user.SerializeToString()), self._revision)

    def _expire_claims(self, node_id: str) -> None:
        claimed = self._claimed.setdefault(node_id, {})
        active = self._active.setdefault(node_id, {})
        now = time.monotonic()
        for token in [token for token, (_, _, until) in claimed.items() if until <= now]:
            email, _, _ = claimed.pop(token)
            active.pop(email, None)

    def _fence_state(self, node_id: str) -> tuple[bool, int]:
        _, until, generation = self._fences.get(node_id, ("", 0, 0))
        return until > time.monotonic(), generation

    async def claim_users(self, node_id: str, worker_id: str, limit: int, lease_seconds: float) -> list[ClaimedUser]:
        if limit <= 0:
            return []
        async with self._lock:
            if self._fence_state(node_id)[0]:
                return []
            self._expire_claims(node_id)
            pending = self._pending.setdefault(node_id, {})
            claimed = self._claimed.setdefault(node_id, {})
            active = self._active.setdefault(node_id, {})
            until = time.monotonic() + lease_seconds
            result: list[ClaimedUser] = []
            ready = (email for email in pending if email not in active)
            for email in islice(ready, limit):
                user, revision = pending[email]
                token = f"{worker_id}:{uuid4()}"
                claimed[token] = (email, revision, until)
                active[email] = token
                result.append(ClaimedUser(token=token, user=User.FromString(user.SerializeToString())))
            return result

    async def ack_users(self, node_id: str, tokens: list[str]) -> None:
        if not tokens:
            return
        async with self._lock:
            self._expire_claims(node_id)
            claimed = self._claimed.setdefault(node_id, {})
            pending = self._pending.setdefault(node_id, {})
            active = self._active.setdefault(node_id, {})
            for token in tokens:
                item = claimed.pop(token, None)
                if item is not None:
                    email, revision, _ = item
                    active.pop(email, None)
                    if email in pending and pending[email][1] == revision:
                        del pending[email]

    async def requeue_users(self, node_id: str, claimed_users: list[ClaimedUser]) -> None:
        if not claimed_users:
            return
        async with self._lock:
            self._expire_claims(node_id)
            claimed = self._claimed.setdefault(node_id, {})
            active = self._active.setdefault(node_id, {})
            for item in claimed_users:
                owned = claimed.pop(item.token, None)
                if owned is not None:
                    active.pop(owned[0], None)

    async def has_pending(self, node_id: str) -> bool:
        """Includes leased work so another worker can recover it after expiry."""
        async with self._lock:
            return bool(self._pending.get(node_id))

    async def begin_full_sync(self, node_id: str, worker_id: str, lease_seconds: float) -> str | None:
        async with self._lock:
            active, generation = self._fence_state(node_id)
            if active:
                return None
            token = f"{worker_id}:{uuid4()}"
            self._fences[node_id] = (token, time.monotonic() + lease_seconds, generation + 1)
            return token

    async def renew_full_sync(self, node_id: str, token: str, lease_seconds: float) -> bool:
        async with self._lock:
            current, until, generation = self._fences.get(node_id, ("", 0, 0))
            if current != token or until <= time.monotonic():
                return False
            self._fences[node_id] = (token, time.monotonic() + lease_seconds, generation)
            return True

    async def end_full_sync(self, node_id: str, token: str) -> None:
        async with self._lock:
            current, _, generation = self._fences.get(node_id, ("", 0, 0))
            if current == token:
                self._fences[node_id] = ("", 0, generation)

    async def fence_state(self, node_id: str) -> tuple[bool, int]:
        async with self._lock:
            return self._fence_state(node_id)

    async def capture_queued(self, node_id: str) -> tuple[dict[str, int], int]:
        async with self._lock:
            self._expire_claims(node_id)
            return (
                {email: revision for email, (_, revision) in self._pending.get(node_id, {}).items()},
                len(self._claimed.get(node_id, {})),
            )

    async def retire_captured(self, node_id: str, captured: dict[str, int]) -> int:
        async with self._lock:
            pending = self._pending.get(node_id, {})
            active = self._active.get(node_id, {})
            retired = 0
            for email, revision in captured.items():
                if email not in active and email in pending and pending[email][1] == revision:
                    del pending[email]
                    retired += 1
            return retired

    async def clear(self, node_id: str) -> None:
        async with self._lock:
            self._pending.pop(node_id, None)
            self._claimed.pop(node_id, None)
            self._active.pop(node_id, None)


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
