# PasarGuard Node Bridge (Python)

Async Python client for connecting to a [PasarGuard node](https://github.com/PasarGuard/node) over `gRPC` or `REST`.

This package provides:
- Strongly typed protobuf models (`service_pb2`)
- Unified node API for both transport types
- User sync helpers (single, batch, and chunked streaming)
- Health/version helpers
- On-demand log streaming
- Node maintenance endpoints (update core/node/geofiles)

## Installation

```bash
pip install pasarguard-node-bridge
```

## Requirements

- Python `>=3.12`
- A reachable PasarGuard node
- Node service port (`port`) for gRPC or protobuf-REST
- Node JSON API port (`api_port`) for maintenance endpoints
- Server CA certificate content (PEM string)
- API key (UUID string)

## Import

```python
import PasarGuardNodeBridge as Bridge
from PasarGuardNodeBridge.common import service_pb2 as service
```

## Create A Node Client

```python
node = Bridge.create_node(
    connection=Bridge.NodeType.grpc,  # Bridge.NodeType.grpc or Bridge.NodeType.rest
    address="127.0.0.1",
    port=2096,                         # gRPC or protobuf-REST port (based on connection)
    api_port=2097,                     # REST JSON API port (used internally for maintenance)
    server_ca=server_ca_pem_string,
    api_key="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    name="node-1",                     # optional
    extra={"region": "eu-1"},          # optional
    default_timeout=10,                # optional
    internal_timeout=15,               # optional
    proxy="socks5://user:pass@127.0.0.1:1080",  # optional
)
```

### `create_node(...)` Parameters

- `connection`: `Bridge.NodeType.grpc` or `Bridge.NodeType.rest`
- `address`: node host/IP
- `port`: node service port
- `api_port`: node REST JSON API port
- `server_ca`: PEM certificate content as string
- `api_key`: UUID string
- `name`: optional logger name
- `extra`: optional metadata dictionary
- `logger`: optional custom logger
- `default_timeout`: default timeout for public API methods
- `internal_timeout`: timeout used for internal sync/log operations
- `proxy`: optional upstream proxy URL for node traffic
- `max_message_size`: gRPC only, HTTP/2 window/message sizing

### Proxy Formats

- `socks5://127.0.0.1:1080`
- `socks5://user:pass@127.0.0.1:1080`
- `socks4://127.0.0.1:1080`
- `http://127.0.0.1:3128`
- `http://user:pass@127.0.0.1:3128`
- `https://user:pass@proxy.example.com:443`

### Connection Types

- `Bridge.NodeType.grpc`: gRPC transport via `grpclib`
- `Bridge.NodeType.rest`: protobuf-over-HTTP transport

## User/Proxy Builders

Use helpers for creating protobuf user/proxy payloads.

```python
user = Bridge.create_user(
    email="alice@example.com",
    proxies=Bridge.create_proxy(
        vmess_id="0d59268a-9847-4218-ae09-65308eb52e08",
        vless_id="0d59268a-9847-4218-ae09-65308eb52e08",
        vless_flow="",
        trojan_password="",
        shadowsocks_password="",
        shadowsocks_method="",
        wireguard_public_key="",
        wireguard_peer_ips=["10.10.0.2/32"],
    ),
    inbounds=["inbound-tag-1"],
)
```

## Start/Stop Lifecycle

You should `start()` before calling stats/sync/log methods.

```python
await node.start(
    config=config_json_string,
    backend_type=service.BackendType.XRAY,   # or service.BackendType.WIREGUARD
    users=[user],                             # optional initial user set
    keep_alive=30,                            # optional
    exclude_inbounds=[],                      # optional
    timeout=20,
)

info = await node.info()
print(info.node_version, info.core_version)

await node.stop()
```

## Method Examples

### 1. Queue-Based User Updates (recommended for frequent updates)

`update_user` and `update_users` enqueue users and a background worker handles retries and batching.
If the configured per-node queue bound is reached, both methods raise
`Bridge.UserSyncStoreFullError`; callers may retry after queued work is processed.

```python
await node.update_user(user)

more_users = [user1, user2, user3]
await node.update_users(more_users)
```

#### Shared Storage For Multiple Workers

By default, queued user updates are kept in a process-local in-memory store shared by node instances. This coordinates controllers in a single worker process when they use the same `node_id` (or the same service URL when `node_id` is omitted). For multi-process or multi-host deployments, pass a shared `user_sync_store` implementation so all workers claim from the same pending-user queue. The package only defines the async protocol; Redis, NATS KV, SQL, or any other backend can be implemented by your application.

```python
store = MyRedisUserSyncStore(redis_client)  # implements Bridge.UserSyncStoreProtocol

node = Bridge.create_node(
    connection=Bridge.NodeType.grpc,
    address="127.0.0.1",
    port=2096,
    api_port=2097,
    server_ca=server_ca_pem_string,
    api_key="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    node_id="node-1",
    worker_id="worker-a",
    user_sync_store=store,
)
```

A `UserSyncStoreProtocol` implementation must provide these async methods:

- `enqueue_users(node_id, users)` stores latest user payloads by email.
- `claim_users(node_id, worker_id, limit, lease_seconds)` atomically leases work and returns `ClaimedUser` items.
- `next_claim_delay(node_id)` returns seconds until tracked work can next be claimed, `0.0` for immediately
  claimable work, or `None` when no work is tracked. This lets an idle worker wake after another worker's lease expires.
- `ack_users(node_id, tokens)` removes successfully synced claims.
- `requeue_users(node_id, claimed_users)` makes failed claims available again.
- `clear(node_id)` clears pending and claimed updates for a node.

Delivery is at-least-once. A crashed worker may cause the same latest user payload to be synced again after its lease expires, so external adapters should use atomic claim/lease operations such as Redis Lua/transactions or NATS KV revision compare-and-set.
For compatibility, stores without `next_claim_delay` are rechecked after at most one configured lease interval, but new
implementations should provide it so workers can preserve the normal fast idle exit when the store is truly empty.

#### Coordinated Permanent User Revocation

Permanent deletion requires stronger ordering than the ordinary at-least-once queue. A
`RevocationAwareUserSyncStoreProtocol` implementation provides per-user generations, execution leases, and
operation-owned fences. Stores without this optional capability continue to support normal updates, while calls to the
revocation API fail closed with `NodeAPIError` code `501`.

```python
revocation_id = "delete-request-42"
user_keys = [user.email]

barrier = await node.begin_user_revocation(user_keys, revocation_id)
users_to_remove = [removed_user] if removed_user.email in barrier.active_user_keys else []
try:
    if users_to_remove:
        failed = await node.sync_users_chunked(users_to_remove, revocation_id=revocation_id)
        if failed:
            raise RuntimeError("revocation update did not complete")
    await commit_database_delete()
except BaseException:
    users_to_restore = (
        [authoritative_restored_user]
        if authoritative_restored_user.email in barrier.active_user_keys
        else []
    )
    if users_to_restore:
        failed = await node.sync_users_chunked(users_to_restore, revocation_id=revocation_id)
        if failed:
            raise RuntimeError("authoritative restore did not complete")
    await node.abort_user_revocation(user_keys, revocation_id)
    raise
else:
    await node.finalize_user_revocation(user_keys, revocation_id)
```

`begin_user_revocation` returns `UserRevocationResult(active_user_keys, finalized_user_keys)`. Direct writes must contain
only `active_user_keys`; already-finalized keys are idempotently skipped. It discards older pending/claimed payloads and
waits for older in-flight user-sync leases to drain. A different operation that already owns any requested key causes
`UserRevocationConflictError`; retry the whole operation later. This fail-fast serialization prevents one operation's
rollback restore from racing another operation's permanent finalize.

An abort must happen only after the authoritative user has been restored on the node. Abort and finalize close admission
before draining authorized writes. A successful finalize leaves a permanent tombstone, and later aborts or stale queue
generations cannot reopen it. Shared stores must keep these fences, generations, claims, and execution leases in the same
atomic consistency domain. A timeout, cancellation, partial transport failure, or expired heartbeat leaves the remote
outcome unknown; its lease is deliberately retained and revocation fails closed. Recover by calling
`reconcile_users(authoritative_users)`: the revocation-aware store waits for live writes, replaces expired poison with a
node-wide permit, and clears it only after the authoritative full snapshot is acknowledged. A failed reconciliation
retains a new node-wide poison lease, so a crash cannot silently reopen revocation.

Node startup and `sync_users` are full replacement snapshots, so their execution leases cover every user on the node,
including users omitted from the request. A snapshot already in flight drains before a new fence opens. A snapshot which
encounters a provisional fence waits for its authoritative abort/finalize outcome; permanently finalized users are then
omitted from the request. An ambiguous snapshot timeout or cancellation retains the node-wide lease and fails every later
write or permanent revocation on that node closed until reconciliation. `sync_users` rejects `revocation_id`; operation-
owned removal and restore writes must use the partial `sync_users_chunked` transport and treat any returned users as a
failure.

Fences are scoped to `node_id`. A controller which adds a new node concurrently with deletion must register that node in
the revocation topology before reading its authoritative startup snapshot; a fence on another node cannot protect it.

Version `0.10.0` adds node-enforced monotonic user-sync epochs plus the required startup-snapshot, epoch-floor handshake,
and atomic lease-narrowing methods to `RevocationAwareUserSyncStoreProtocol`. Roll out the Node binary first, but do not
activate positive epochs yet. Then stop or drain **all** legacy Panel/Bridge workers, upgrade every shared-store adapter
and Bridge process together, and only then resume user mutations and permanent revocation. After a Node accepts its first
positive epoch it rejects legacy epoch-zero clients with HTTP `412` / gRPC `FailedPrecondition`; mixed old/new workers and
rollback to Bridge `0.9` are intentionally unsupported until the Node service is fully restarted and its backend is
recreated from an authoritative snapshot. A Bridge capability probe reads the Node's current epoch and atomically advances
the shared allocator before granting a write lease, so a restarted worker cannot reuse a stale lower epoch. Do not run
user sync or permanent revocation during this cutover.

Lifecycle operations are coordinated through the same model. The default process-local coordinator prevents concurrent `start()`, `stop()`, `update_node()`, `update_core()`, and `update_geofiles()` calls from controllers for the same node in one process. Pass a shared `lifecycle_coordinator` in multi-process or multi-host deployments so only one worker can perform a lifecycle operation at a time. Read-only status cron jobs can call stats/info normally; if they write shared observed status, use the current lifecycle epoch so stale cron results cannot overwrite a newer reconnect result.

```python
lifecycle = MyRedisLifecycleCoordinator(redis_client)

node = Bridge.create_node(
    connection=Bridge.NodeType.grpc,
    address="127.0.0.1",
    port=2096,
    api_port=2097,
    server_ca=server_ca_pem_string,
    api_key="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    node_id="node-1",
    worker_id="worker-a",
    user_sync_store=store,
    lifecycle_coordinator=lifecycle,
)

state = await node.get_lifecycle_state()
health = await node.get_health()
if state is not None:
    await node.update_observed_lifecycle(
        Bridge.LifecycleStatus.HEALTHY if health is Bridge.Health.HEALTHY else Bridge.LifecycleStatus.BROKEN,
        expected_epoch=state.epoch,
    )
```

A lifecycle adapter must atomically acquire/release leases, return `False` when heartbeat ownership is lost, and fence
writes with the returned epoch. An expired lease is an unknown remote effect and must not be stolen. Calling
`reconcile_lifecycle(observed_status)` is safe only after an operator has independently established that the old worker and
request can no longer complete; a status probe alone is not such a guarantee. Reconciliation refuses to clear a still-live
lease. Applications must not automatically reconcile a timeout and launch a competing lifecycle operation, because the
Node management API does not yet carry a server-enforced lifecycle fencing token.

The built-in lifecycle coordinator is shared only by controllers in the same Python process. Deployments with more than
one Panel/Bridge process must inject the same durable shared adapter (for example, backed by Redis or a transactional
database) into every controller. Adapter failures are exposed by lifecycle APIs as `NodeAPIError` with code `503`.
`NodeLifecycleState.updated_at` is a Unix wall-clock timestamp; adapters should use a monotonic clock separately for lease
expiry calculations and must not persist process-local monotonic values as public state.

Node connection configs can also be stored through a registry protocol:

```python
registry = MyNodeRegistry(...)
config = Bridge.NodeConfig(
    connection="grpc",
    address="127.0.0.1",
    port=2096,
    api_port=2097,
    server_ca=server_ca_pem_string,
    api_key="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
)

await Bridge.save_node_config(registry, "node-1", config)
node = await Bridge.create_node_from_registry(
    registry,
    "node-1",
    user_sync_store=store,
    worker_id="worker-a",
)
```

### 2. Full User Snapshot Sync

`sync_users` replaces the node's complete user set. Always pass the authoritative full snapshot; an empty list clears all
users. Use `sync_users_chunked` for partial updates.

```python
await node.sync_users([user1, user2], timeout=15)
```

### 3. Chunked Sync For Large Batches

```python
failed_users = await node.sync_users_chunked(
    users=large_user_list,
    chunk_size=500,
    timeout=30,
)

if failed_users:
    print(f"Failed users: {len(failed_users)}")
```

### 4. Stats APIs

```python
system_stats = await node.get_system_stats()
backend_stats = await node.get_backend_stats()
latencies = await node.get_outbounds_latency()

all_outbounds = await node.get_stats(
    stat_type=service.StatType.Outbounds,
    reset=False,
)

single_user_online = await node.get_user_online_stats("alice@example.com")
single_user_ips = await node.get_user_online_ip_list("alice@example.com")
```

### 5. Health And Version Helpers

```python
health = await node.get_health()            # Bridge.Health enum
node_ver = await node.node_version()
core_ver = await node.core_version()
node_ver2, core_ver2 = await node.get_versions()
meta = await node.get_extra()

# Backwards-compatible synchronous metadata attribute. Prefer get_extra() in
# new asynchronous code.
legacy_meta = node.extra
```

### 6. On-Demand Log Streaming

`stream_logs()` yields an `asyncio.Queue` that contains log lines (`str`) or `Bridge.NodeAPIError`.

```python
import asyncio

async with node.stream_logs(max_queue_size=200) as log_queue:
    for _ in range(20):
        item = await asyncio.wait_for(log_queue.get(), timeout=2)
        if isinstance(item, Bridge.NodeAPIError):
            raise item
        print(item)
```

### 7. Maintenance Endpoints

These methods use the node REST JSON API (`api_port`).

```python
await node.update_node()
await node.update_core({"version": "latest"})
await node.update_geofiles({"remove_temp": True})
```

### 8. Routing APIs

Routing operations work over both gRPC and REST. They are xray-only: on a non-xray
(e.g. WireGuard) node the call fails with `Bridge.NodeAPIError` code `501`.

```python
rules = await node.list_routing_rules()
balancer = await node.get_balancer_info("balancer-tag")

route = await node.test_route(
    inbound_tag="inbound-1",
    network="tcp",
    target_domain="example.com",
    target_port=443,
)

# `rule` is one xray routing rule as JSON (same shape as a routing.rules[] entry).
# Appended by default (keeps existing rules); pass should_reset=True to clear all
# rules + balancers before adding.
await node.add_routing_rule(
    '{"type":"field","outboundTag":"direct","domain":["example.com"],"ruleTag":"r1"}'
)
await node.remove_routing_rule("r1")
await node.override_balancer_target("balancer-tag", "outbound-tag")
```

## API Reference

### Lifecycle

- `start(config, backend_type, users, keep_alive=0, exclude_inbounds=[], timeout=None)`
- `stop(timeout=None)`
- `info(timeout=None)`

### Health/Version

- `get_health()`
- `node_version()`
- `core_version()`
- `get_versions()`
- `get_extra()`

### Stats

- `get_system_stats(timeout=None)`
- `get_backend_stats(timeout=None)`
- `get_stats(stat_type, reset=True, name="", timeout=None)`
- `get_outbounds_latency(name="", timeout=None)`
- `get_user_online_stats(email, timeout=None)`
- `get_user_online_ip_list(email, timeout=None)`

### User Sync

- `update_user(user)` (queued/background)
- `update_users(users)` (queued/background)
- `begin_user_revocation(user_keys, revocation_id)` → `UserRevocationResult`
- `abort_user_revocation(user_keys, revocation_id)` (release this provisional fence after restore)
- `finalize_user_revocation(user_keys, revocation_id)` (commit permanent tombstones)
- `sync_users(users, flush_pending=False, timeout=None, revocation_id=None)` (full replacement; `revocation_id` rejected)
- `reconcile_users(users, flush_pending=False, timeout=None)` (authoritative full replacement that recovers expired/unknown sync leases)
- `start(..., reconcile_user_sync=True)` (authoritative startup snapshot recovery after a crashed/expired writer)
- `sync_users_chunked(users, chunk_size=100, flush_pending=False, timeout=None, revocation_id=None)` (partial streaming)

### Routing

Xray-only (gRPC and REST); on a non-xray backend these raise `NodeAPIError(501)`.

- `list_routing_rules(timeout=None)`
- `get_balancer_info(tag, timeout=None)`
- `test_route(inbound_tag="", network="", target_ip="", target_domain="", target_port=0, protocol="", user="", attributes=None, field_selectors=None, publish_result=False, timeout=None)`
- `add_routing_rule(rule, should_reset=False, timeout=None)`
- `remove_routing_rule(rule_tag, timeout=None)`
- `override_balancer_target(balancer_tag, target, timeout=None)`

### Logging

- `stream_logs(max_queue_size=1000)` async context manager returning an `asyncio.Queue`

### Maintenance

- `update_node()`
- `update_core(json)`
- `update_geofiles(json)`

## Error Handling

All transport and API errors are surfaced as `Bridge.NodeAPIError`:

```python
try:
    await node.get_backend_stats(timeout=5)
except Bridge.NodeAPIError as e:
    print(e.code, e.detail)
```

Local queue-capacity errors from `update_user` and `update_users` are surfaced
separately as `Bridge.UserSyncStoreFullError` so callers can apply backpressure.

## Protobuf Access

For direct protobuf usage:

```python
from PasarGuardNodeBridge.common import service_pb2 as service
```

## Complete Minimal Example

```python
import asyncio
import PasarGuardNodeBridge as Bridge
from PasarGuardNodeBridge.common import service_pb2 as service


async def main():
    with open("certs/ssl_cert.pem", "r", encoding="utf-8") as f:
        server_ca = f.read()
    with open("config/xray.json", "r", encoding="utf-8") as f:
        config = f.read()

    node = Bridge.create_node(
        connection=Bridge.NodeType.grpc,
        address="127.0.0.1",
        port=2096,
        api_port=2097,
        server_ca=server_ca,
        api_key="d04d8680-942d-4365-992f-9f482275691d",
        name="example-node",
    )

    await node.start(config=config, backend_type=service.BackendType.XRAY, users=[])
    print(await node.get_system_stats())
    await node.stop()


asyncio.run(main())
```
