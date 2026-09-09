import asyncio
import time
from dataclasses import asdict, dataclass, field, replace
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
    generation: int = 0


@dataclass(slots=True)
class UserSyncLease:
    """A revocation-aware permit for applying user updates to a node."""

    node_id: str
    worker_id: str
    token: str
    user_keys: tuple[str, ...]
    generations: dict[str, int]
    lease_seconds: float = 30.0
    revocation_id: str | None = None
    covers_all_users: bool = False
    epoch: int = 0


@dataclass(slots=True)
class StartupUserSyncLease:
    """A node-wide startup snapshot permit and the keys safe to include."""

    lease: UserSyncLease
    included_user_keys: tuple[str, ...]


@dataclass(slots=True)
class UserRevocationResult:
    """Keys acquired by this operation and keys already permanently revoked."""

    active_user_keys: tuple[str, ...]
    finalized_user_keys: tuple[str, ...]


class UserSyncStoreFullError(RuntimeError):
    """Raised when accepting more distinct pending users would exceed the store bound."""


class UserSyncLeaseLostError(RuntimeError):
    """Raised when an expired execution lease leaves a user-sync outcome unknown."""


class LifecycleLeaseLostError(RuntimeError):
    """Raised when lifecycle ownership is lost before the remote effect is known."""


class UserRevocationConflictError(RuntimeError):
    """Raised when another operation owns one or more requested user fences."""

    def __init__(self, conflicting_user_keys: tuple[str, ...]):
        self.conflicting_user_keys = conflicting_user_keys
        super().__init__(f"another revocation owns {len(conflicting_user_keys)} user fence(s)")


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

    async def next_claim_delay(self, node_id: str) -> float | None: ...

    async def ack_users(self, node_id: str, tokens: list[str]) -> None: ...
    async def requeue_users(self, node_id: str, claimed_users: list[ClaimedUser]) -> None: ...
    async def clear(self, node_id: str) -> None: ...


class RevocationAwareUserSyncStoreProtocol(UserSyncStoreProtocol, Protocol):
    """Optional store capability required for coordinated permanent revocation.

    ``begin_user_revocation`` must atomically fence the supplied keys, discard
    their pending and claimed payloads, and wait for intersecting unexpired
    execution leases to drain. Implementations must also reject enqueue, claim,
    and requeue attempts for fenced keys or obsolete generations. Expired
    execution leases have an unknown remote outcome and must fail revocation
    closed until explicitly reconciled; they cannot be silently discarded.

    Distinct operation IDs must be serialized for intersecting keys. A store
    may fail fast with ``UserRevocationConflictError``; it must not leave a
    partially acquired multi-key fence. Abort and finalize close authorized
    admission before draining all intersecting execution leases.

    Passing ``revocation_id`` to ``acquire_user_sync_lease`` authorizes only
    keys provisionally owned by that operation. This is required for the direct
    removal/restore writes performed while the ordinary update fence is active.
    """

    async def begin_user_revocation(
        self, node_id: str, user_keys: list[str], revocation_id: str
    ) -> UserRevocationResult: ...

    async def abort_user_revocation(self, node_id: str, user_keys: list[str], revocation_id: str) -> None: ...

    async def finalize_user_revocation(self, node_id: str, user_keys: list[str], revocation_id: str) -> None: ...

    async def acquire_user_sync_lease(
        self,
        node_id: str,
        worker_id: str,
        user_keys: list[str],
        lease_seconds: float,
        expected_generations: dict[str, int] | None = None,
        revocation_id: str | None = None,
    ) -> UserSyncLease: ...

    async def acquire_startup_user_sync_lease(
        self,
        node_id: str,
        worker_id: str,
        user_keys: list[str],
        lease_seconds: float,
    ) -> StartupUserSyncLease: ...

    async def acquire_user_sync_reconciliation_lease(
        self,
        node_id: str,
        worker_id: str,
        user_keys: list[str],
        lease_seconds: float,
    ) -> StartupUserSyncLease:
        """Replace expired unknown writes with one authoritative snapshot."""
        ...

    async def advance_user_sync_epoch(self, node_id: str, minimum_epoch: int) -> None:
        """Atomically advance the next-epoch floor from a Node handshake."""
        ...

    async def retain_user_sync_lease_keys(
        self, lease: UserSyncLease, retained_user_keys: list[str]
    ) -> UserSyncLease: ...

    async def heartbeat_user_sync_lease(self, lease: UserSyncLease) -> bool: ...
    async def release_user_sync_lease(self, lease: UserSyncLease) -> None: ...


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
    async def heartbeat(self, lease: LifecycleLease) -> bool: ...
    async def get_state(self, node_id: str) -> NodeLifecycleState | None: ...

    async def reconcile(self, node_id: str, observed: LifecycleStatus) -> bool:
        """Clear an expired unknown lease after the remote state was inspected."""
        ...

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


@dataclass(slots=True)
class _UserRevocationState:
    generation: int = 0
    active_owner: str | None = None
    closing: bool = False
    finalized: bool = False


class InMemoryUserSyncStore:
    def __init__(self, max_pending_users_per_node: int = 10_000):
        if max_pending_users_per_node <= 0:
            raise ValueError("max_pending_users_per_node must be positive")
        self._max_pending_users_per_node = max_pending_users_per_node
        self._pending: dict[str, dict[str, tuple[User, int]]] = {}
        self._claimed: dict[str, dict[str, tuple[User, int, float]]] = {}
        self._lock = asyncio.Lock()
        self._lease_changed = asyncio.Condition(self._lock)
        self._revocations: dict[str, dict[str, _UserRevocationState]] = {}
        self._user_sync_leases: dict[str, tuple[UserSyncLease, float]] = {}
        self._user_sync_epochs: dict[str, int] = {}
        self._startup_pending: set[str] = set()

    @staticmethod
    def _unique_user_keys(user_keys: list[str]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(user_keys))

    def _revocation_state(self, node_id: str, user_key: str) -> _UserRevocationState:
        states = self._revocations.setdefault(node_id, {})
        return states.setdefault(user_key, _UserRevocationState())

    def _is_user_fenced(self, node_id: str, user_key: str) -> bool:
        state = self._revocations.get(node_id, {}).get(user_key)
        return state is not None and (state.finalized or state.active_owner is not None)

    def _is_user_finalized(self, node_id: str, user_key: str) -> bool:
        """Read a tombstone without allocating state for an unseen user."""
        state = self._revocations.get(node_id, {}).get(user_key)
        return state is not None and state.finalized

    def _user_generation(self, node_id: str, user_key: str) -> int:
        state = self._revocations.get(node_id, {}).get(user_key)
        return state.generation if state is not None else 0

    def _next_user_sync_epoch(self, node_id: str) -> int:
        epoch = self._user_sync_epochs.get(node_id, 0) + 1
        self._user_sync_epochs[node_id] = epoch
        return epoch

    def _next_intersecting_lease_delay(
        self,
        node_id: str,
        user_keys: set[str],
        now: float,
        revocation_id: str | None = None,
    ) -> float | None:
        delays = [
            max(0.0, expires_at - now)
            for lease, expires_at in self._user_sync_leases.values()
            if lease.node_id == node_id
            and (lease.covers_all_users or user_keys.intersection(lease.user_keys))
            and (revocation_id is None or lease.revocation_id == revocation_id)
        ]
        return min(delays) if delays else None

    def _has_lost_user_sync_lease(
        self,
        node_id: str,
        user_keys: set[str],
        now: float,
        revocation_id: str | None = None,
    ) -> bool:
        return any(
            lease.node_id == node_id
            and expires_at <= now
            and (lease.covers_all_users or user_keys.intersection(lease.user_keys))
            and (revocation_id is None or lease.revocation_id == revocation_id)
            for lease, expires_at in self._user_sync_leases.values()
        )

    async def _wait_for_user_sync_leases(
        self,
        node_id: str,
        user_keys: set[str],
        revocation_id: str | None = None,
        covers_all_users: bool = False,
    ) -> None:
        while True:
            now = time.monotonic()
            if covers_all_users:
                matching_leases = [
                    (lease, expires_at)
                    for lease, expires_at in self._user_sync_leases.values()
                    if lease.node_id == node_id and (revocation_id is None or lease.revocation_id == revocation_id)
                ]
                if any(expires_at <= now for _, expires_at in matching_leases):
                    raise UserSyncLeaseLostError("an expired user-sync execution lease has an unknown remote outcome")
                delay = min((max(0.0, expires_at - now) for _, expires_at in matching_leases), default=None)
            elif self._has_lost_user_sync_lease(node_id, user_keys, now, revocation_id):
                raise UserSyncLeaseLostError("an expired user-sync execution lease has an unknown remote outcome")
            else:
                delay = self._next_intersecting_lease_delay(node_id, user_keys, now, revocation_id)
            if delay is None:
                return
            try:
                await asyncio.wait_for(self._lease_changed.wait(), timeout=max(delay, 0.001))
            except TimeoutError:
                pass

    async def _wait_for_startup_completion(self, node_id: str) -> None:
        """Wait until no startup is acquiring or holding a node-wide permit."""
        while True:
            if node_id in self._startup_pending:
                await self._lease_changed.wait()
                continue
            now = time.monotonic()
            startup_leases = [
                (lease, expires_at)
                for lease, expires_at in self._user_sync_leases.values()
                if lease.node_id == node_id and lease.covers_all_users
            ]
            if any(expires_at <= now for _, expires_at in startup_leases):
                raise UserSyncLeaseLostError("an expired startup execution lease has an unknown remote outcome")
            if not startup_leases:
                return
            delay = min(max(0.0, expires_at - now) for _, expires_at in startup_leases)
            try:
                await asyncio.wait_for(self._lease_changed.wait(), timeout=max(delay, 0.001))
            except TimeoutError:
                pass

    async def enqueue_users(self, node_id: str, users: list[User]) -> None:
        if not users:
            return
        async with self._lock:
            pending = self._pending.setdefault(node_id, {})
            claimed = self._claimed.setdefault(node_id, {})
            latest_users = {user.email: user for user in users if not self._is_user_fenced(node_id, user.email)}
            if not latest_users:
                return
            tracked_emails = set(pending)
            tracked_emails.update(user.email for user, _, _ in claimed.values())
            new_emails = set(latest_users).difference(tracked_emails)
            if len(tracked_emails) + len(new_emails) > self._max_pending_users_per_node:
                raise UserSyncStoreFullError(
                    f"pending user sync limit reached for node ({self._max_pending_users_per_node})"
                )
            for user in latest_users.values():
                pending[user.email] = (user, self._user_generation(node_id, user.email))

    async def claim_users(self, node_id: str, worker_id: str, limit: int, lease_seconds: float) -> list[ClaimedUser]:
        if limit <= 0:
            return []
        now = time.monotonic()
        async with self._lock:
            pending = self._pending.setdefault(node_id, {})
            claimed = self._claimed.setdefault(node_id, {})

            for token, (user, generation, expires_at) in list(claimed.items()):
                if expires_at <= now:
                    if not self._is_user_fenced(node_id, user.email) and generation == self._user_generation(
                        node_id, user.email
                    ):
                        pending.setdefault(user.email, (user, generation))
                    del claimed[token]

            result: list[ClaimedUser] = []
            for email, (user, generation) in list(pending.items()):
                if self._is_user_fenced(node_id, email) or generation != self._user_generation(node_id, email):
                    del pending[email]
                    continue
                token = f"{worker_id}:{uuid4()}"
                claimed[token] = (user, generation, now + lease_seconds)
                result.append(ClaimedUser(token=token, user=user, generation=generation))
                del pending[email]
                if len(result) >= limit:
                    break
            return result

    async def next_claim_delay(self, node_id: str) -> float | None:
        """Return when tracked work can next be claimed, or ``None`` when none exists."""
        now = time.monotonic()
        async with self._lock:
            pending = self._pending.get(node_id)
            if pending:
                return 0.0

            claimed = self._claimed.get(node_id)
            if not claimed:
                return None

            return max(0.0, min(expires_at for _, _, expires_at in claimed.values()) - now)

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
                owned_claim = claimed.pop(item.token, None)
                if owned_claim is not None:
                    user, generation, _ = owned_claim
                    if (
                        generation == item.generation
                        and generation == self._user_generation(node_id, user.email)
                        and not self._is_user_fenced(node_id, user.email)
                    ):
                        pending.setdefault(user.email, (user, generation))

    async def clear(self, node_id: str) -> None:
        async with self._lock:
            self._pending.pop(node_id, None)
            self._claimed.pop(node_id, None)

    async def begin_user_revocation(
        self, node_id: str, user_keys: list[str], revocation_id: str
    ) -> UserRevocationResult:
        if not revocation_id:
            raise ValueError("revocation_id must not be empty")
        unique_keys = self._unique_user_keys(user_keys)
        if not unique_keys:
            return UserRevocationResult((), ())

        async with self._lease_changed:
            await self._wait_for_startup_completion(node_id)
            active_keys = tuple(
                user_key for user_key in unique_keys if not self._revocation_state(node_id, user_key).finalized
            )
            states = [self._revocation_state(node_id, user_key) for user_key in active_keys]
            conflicting_keys = tuple(
                user_key
                for user_key, state in zip(active_keys, states)
                if state.closing or state.active_owner not in (None, revocation_id)
            )
            if conflicting_keys:
                raise UserRevocationConflictError(conflicting_keys)

            for state in states:
                if state.active_owner is None:
                    state.generation += 1
                    state.active_owner = revocation_id

            pending = self._pending.setdefault(node_id, {})
            for user_key in active_keys:
                pending.pop(user_key, None)

            claimed = self._claimed.setdefault(node_id, {})
            for token, (user, _, _) in list(claimed.items()):
                if user.email in active_keys:
                    claimed.pop(token, None)

            await self._wait_for_user_sync_leases(node_id, set(active_keys))
            finalized_keys = tuple(user_key for user_key in unique_keys if user_key not in active_keys)
            return UserRevocationResult(active_keys, finalized_keys)

    async def abort_user_revocation(self, node_id: str, user_keys: list[str], revocation_id: str) -> None:
        if not revocation_id:
            raise ValueError("revocation_id must not be empty")
        unique_keys = self._unique_user_keys(user_keys)
        async with self._lease_changed:
            affected_keys = {
                user_key
                for user_key in unique_keys
                if (state := self._revocations.get(node_id, {}).get(user_key)) is not None
                and not state.finalized
                and state.active_owner == revocation_id
            }
            if not affected_keys:
                return
            for user_key in affected_keys:
                self._revocation_state(node_id, user_key).closing = True
            try:
                await self._wait_for_user_sync_leases(node_id, affected_keys)
            except BaseException:
                for user_key in affected_keys:
                    state = self._revocation_state(node_id, user_key)
                    if state.active_owner == revocation_id and not state.finalized:
                        state.closing = False
                if affected_keys:
                    self._lease_changed.notify_all()
                raise
            for user_key in affected_keys:
                state = self._revocation_state(node_id, user_key)
                state.active_owner = None
                state.closing = False
            if affected_keys:
                self._lease_changed.notify_all()

    async def finalize_user_revocation(self, node_id: str, user_keys: list[str], revocation_id: str) -> None:
        if not revocation_id:
            raise ValueError("revocation_id must not be empty")
        unique_keys = self._unique_user_keys(user_keys)
        async with self._lease_changed:
            affected_keys = {
                user_key
                for user_key in unique_keys
                if (state := self._revocations.get(node_id, {}).get(user_key)) is not None
                and not state.finalized
                and state.active_owner == revocation_id
            }
            if not affected_keys:
                return
            for user_key in affected_keys:
                state = self._revocation_state(node_id, user_key)
                state.closing = True
            try:
                await self._wait_for_user_sync_leases(node_id, affected_keys)
            except BaseException:
                for user_key in affected_keys:
                    state = self._revocation_state(node_id, user_key)
                    if state.active_owner == revocation_id and not state.finalized:
                        state.closing = False
                if affected_keys:
                    self._lease_changed.notify_all()
                raise
            for user_key in affected_keys:
                state = self._revocation_state(node_id, user_key)
                state.finalized = True
                state.active_owner = None
                state.closing = False
            if affected_keys:
                self._lease_changed.notify_all()

    async def acquire_user_sync_lease(
        self,
        node_id: str,
        worker_id: str,
        user_keys: list[str],
        lease_seconds: float,
        expected_generations: dict[str, int] | None = None,
        revocation_id: str | None = None,
    ) -> UserSyncLease:
        unique_keys = self._unique_user_keys(user_keys)
        async with self._lease_changed:
            await self._wait_for_startup_completion(node_id)
            now = time.monotonic()
            allowed_keys = tuple(
                key
                for key in unique_keys
                if (
                    (
                        (state := self._revocation_state(node_id, key)).active_owner == revocation_id
                        and not state.closing
                        and not state.finalized
                    )
                    if revocation_id is not None
                    else not self._is_user_fenced(node_id, key)
                )
                and (
                    expected_generations is None or expected_generations.get(key) == self._user_generation(node_id, key)
                )
            )
            generations = {key: self._user_generation(node_id, key) for key in allowed_keys}
            token = f"{worker_id}:{uuid4()}" if allowed_keys else ""
            lease = UserSyncLease(
                node_id=node_id,
                worker_id=worker_id,
                token=token,
                user_keys=allowed_keys,
                generations=generations,
                epoch=self._next_user_sync_epoch(node_id) if token else 0,
                lease_seconds=lease_seconds,
                revocation_id=revocation_id,
            )
            if token:
                self._user_sync_leases[token] = (lease, now + lease_seconds)
            return lease

    async def advance_user_sync_epoch(self, node_id: str, minimum_epoch: int) -> None:
        if minimum_epoch < 0:
            raise ValueError("minimum_epoch must be non-negative")
        async with self._lock:
            self._user_sync_epochs[node_id] = max(
                self._user_sync_epochs.get(node_id, 0),
                minimum_epoch,
            )

    async def acquire_startup_user_sync_lease(
        self,
        node_id: str,
        worker_id: str,
        user_keys: list[str],
        lease_seconds: float,
    ) -> StartupUserSyncLease:
        """Serialize a full replacement snapshot with every per-user write.

        A startup snapshot is node-wide: a key omitted from the payload is a
        deletion just as surely as a key present in it is an update.  Wait for
        provisional revocations to reach abort/finalize, then atomically take a
        wildcard execution lease before deciding which finalized keys to omit.
        """
        unique_keys = self._unique_user_keys(user_keys)
        async with self._lease_changed:
            while node_id in self._startup_pending:
                await self._lease_changed.wait()
            while any(state.active_owner is not None for state in self._revocations.get(node_id, {}).values()):
                await self._lease_changed.wait()
                while node_id in self._startup_pending:
                    await self._lease_changed.wait()
            self._startup_pending.add(node_id)
            try:
                await self._wait_for_user_sync_leases(node_id, set(), covers_all_users=True)

                now = time.monotonic()
                included_keys = tuple(key for key in unique_keys if not self._is_user_finalized(node_id, key))
                generations = {key: self._user_generation(node_id, key) for key in unique_keys}
                token = f"{worker_id}:{uuid4()}"
                lease = UserSyncLease(
                    node_id=node_id,
                    worker_id=worker_id,
                    token=token,
                    user_keys=unique_keys,
                    generations=generations,
                    epoch=self._next_user_sync_epoch(node_id),
                    lease_seconds=lease_seconds,
                    covers_all_users=True,
                )
                self._user_sync_leases[token] = (lease, now + lease_seconds)
                return StartupUserSyncLease(lease=lease, included_user_keys=included_keys)
            finally:
                self._startup_pending.discard(node_id)
                self._lease_changed.notify_all()

    async def acquire_user_sync_reconciliation_lease(
        self,
        node_id: str,
        worker_id: str,
        user_keys: list[str],
        lease_seconds: float,
    ) -> StartupUserSyncLease:
        """Atomically supersede expired unknown writes with a full snapshot.

        Ordinary startup remains fail-closed on an expired execution lease.
        This explicit recovery path waits for all still-live writes, removes
        only expired records for this node, and then holds a node-wide permit
        while the caller applies an authoritative replacement snapshot.  If
        that replacement is ambiguous, its own wildcard lease is abandoned and
        recovery remains fail-closed.
        """
        unique_keys = self._unique_user_keys(user_keys)
        async with self._lease_changed:
            while node_id in self._startup_pending:
                await self._lease_changed.wait()
            while any(state.active_owner is not None for state in self._revocations.get(node_id, {}).values()):
                await self._lease_changed.wait()
                while node_id in self._startup_pending:
                    await self._lease_changed.wait()
            self._startup_pending.add(node_id)
            try:
                while True:
                    now = time.monotonic()
                    node_leases = [
                        (token, lease, expires_at)
                        for token, (lease, expires_at) in self._user_sync_leases.items()
                        if lease.node_id == node_id
                    ]
                    live = [(lease, expires_at) for _, lease, expires_at in node_leases if expires_at > now]
                    if not live:
                        for token, _, _ in node_leases:
                            self._user_sync_leases.pop(token, None)
                        break
                    delay = min(max(0.001, expires_at - now) for _, expires_at in live)
                    try:
                        await asyncio.wait_for(self._lease_changed.wait(), timeout=delay)
                    except TimeoutError:
                        pass

                now = time.monotonic()
                included_keys = tuple(key for key in unique_keys if not self._is_user_finalized(node_id, key))
                generations = {key: self._user_generation(node_id, key) for key in unique_keys}
                token = f"{worker_id}:{uuid4()}"
                lease = UserSyncLease(
                    node_id=node_id,
                    worker_id=worker_id,
                    token=token,
                    user_keys=unique_keys,
                    generations=generations,
                    epoch=self._next_user_sync_epoch(node_id),
                    lease_seconds=lease_seconds,
                    covers_all_users=True,
                )
                self._user_sync_leases[token] = (lease, now + lease_seconds)
                return StartupUserSyncLease(lease=lease, included_user_keys=included_keys)
            finally:
                self._startup_pending.discard(node_id)
                self._lease_changed.notify_all()

    async def retain_user_sync_lease_keys(self, lease: UserSyncLease, retained_user_keys: list[str]) -> UserSyncLease:
        """Atomically narrow a lease after a partial remote outcome."""
        retained_keys = self._unique_user_keys(retained_user_keys)
        if lease.covers_all_users:
            raise ValueError("a node-wide startup lease cannot be narrowed")
        if not retained_keys or not set(retained_keys).issubset(lease.user_keys):
            raise ValueError("retained_user_keys must be a non-empty subset of the lease")
        async with self._lease_changed:
            current = self._user_sync_leases.get(lease.token)
            if current is None or current[0] != lease:
                raise UserSyncLeaseLostError("user-sync execution lease is no longer owned")
            narrowed = replace(
                lease,
                user_keys=retained_keys,
                generations={key: lease.generations[key] for key in retained_keys},
            )
            self._user_sync_leases[lease.token] = (narrowed, current[1])
            self._lease_changed.notify_all()
            return narrowed

    async def heartbeat_user_sync_lease(self, lease: UserSyncLease) -> bool:
        if not lease.token:
            return False
        async with self._lock:
            current = self._user_sync_leases.get(lease.token)
            if current is not None and current[0] == lease and current[1] > time.monotonic():
                self._user_sync_leases[lease.token] = (lease, time.monotonic() + lease.lease_seconds)
                return True
            return False

    async def release_user_sync_lease(self, lease: UserSyncLease) -> None:
        if not lease.token:
            return
        async with self._lease_changed:
            current = self._user_sync_leases.get(lease.token)
            if current is not None and current[0] == lease:
                self._user_sync_leases.pop(lease.token, None)
                self._lease_changed.notify_all()


class InMemoryNodeLifecycleCoordinator:
    def __init__(self):
        self._states: dict[str, NodeLifecycleState] = {}
        self._leases: dict[str, tuple[LifecycleLease, float]] = {}
        self._lock = asyncio.Lock()

    async def try_acquire(
        self, node_id: str, worker_id: str, operation: LifecycleOperation, lease_seconds: float
    ) -> LifecycleLease | None:
        now = time.monotonic()
        wall_time = time.time()
        async with self._lock:
            current = self._leases.get(node_id)
            if current is not None:
                # Never steal an expired lease.  Its remote request may still
                # complete after the local timeout, so allowing a second
                # lifecycle effect would violate operation ordering.
                return None

            state = self._states.get(node_id) or NodeLifecycleState(updated_at=wall_time)
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
            state.updated_at = wall_time
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
        wall_time = time.time()
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
            state.updated_at = wall_time
            self._states[lease.node_id] = state

    async def heartbeat(self, lease: LifecycleLease) -> bool:
        now = time.monotonic()
        async with self._lock:
            current = self._leases.get(lease.node_id)
            if current is not None and current[0].token == lease.token and current[1] > now:
                self._leases[lease.node_id] = (lease, now + lease.lease_seconds)
                return True
            return False

    async def reconcile(self, node_id: str, observed: LifecycleStatus) -> bool:
        """Clear only an expired lease after an authoritative state probe."""
        now = time.monotonic()
        wall_time = time.time()
        async with self._lock:
            current = self._leases.get(node_id)
            if current is not None and current[1] > now:
                return False
            if current is not None:
                lease = current[0]
                epoch = lease.epoch + 1
                self._leases.pop(node_id, None)
            else:
                epoch = (self._states.get(node_id) or NodeLifecycleState()).epoch + 1
            state = self._states.get(node_id) or NodeLifecycleState()
            state.epoch = epoch
            state.desired = observed
            state.observed = observed
            state.operation = None
            state.owner = None
            state.updated_at = wall_time
            self._states[node_id] = state
            return True

    async def get_state(self, node_id: str) -> NodeLifecycleState | None:
        async with self._lock:
            return self._states.get(node_id)

    async def update_observed(self, node_id: str, observed: LifecycleStatus, expected_epoch: int | None = None) -> None:
        wall_time = time.time()
        async with self._lock:
            state = self._states.get(node_id) or NodeLifecycleState(updated_at=wall_time)
            if expected_epoch is not None and state.epoch != expected_epoch:
                return
            state.observed = observed
            state.updated_at = wall_time
            self._states[node_id] = state


_default_user_sync_store = InMemoryUserSyncStore()
_default_lifecycle_coordinator = InMemoryNodeLifecycleCoordinator()


def get_default_user_sync_store() -> InMemoryUserSyncStore:
    """Return the process-local store shared by default controller instances."""
    return _default_user_sync_store


def get_default_lifecycle_coordinator() -> InMemoryNodeLifecycleCoordinator:
    """Return the process-local coordinator shared by default controller instances."""
    return _default_lifecycle_coordinator
