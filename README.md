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

The worker claims at most `sync_batch_size` users per delivery (default: 100). Nodes
supporting chunked sync (`>=0.2.0`) use that transport even for small updates;
older nodes retain the per-user transport. `sync_chunk_size` controls the number
of users per stream message (default: 100). Both options are positive integers
accepted by `create_node(...)` and `create_node_from_config(..., **runtime_overrides)`.

`sync_lease_seconds` (default: 30) bounds the entire queued delivery, including
storage reads, an optional resolver, waiting for the transport lock, and sending
all chunks. Delivery is cancelled before the lease expires. Failed deliveries
and storage errors retry with exponential backoff up to 30 seconds.

`disconnect()` stops this controller's worker and preserves shared queued work.
`connect()` wakes the worker to recover it. An external producer that writes
directly to the store should also notify a healthy controller with
`await node.wake_sync_worker()`; an idle controller is not a permanent queue poller.

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
- `ack_users(node_id, tokens)` removes successfully synced claims.
- `requeue_users(node_id, claimed_users)` makes failed claims available again.
- `clear(node_id)` clears pending and claimed updates for a node.

Delivery is at-least-once. A crashed worker may cause the same latest user payload to be synced again after its lease expires, so external adapters should use atomic claim/lease operations such as Redis Lua/transactions or NATS KV revision compare-and-set.

Claims must exclude concurrent delivery of the same email until release or lease
expiry. Acknowledging an older revision must preserve a newer queued update;
requeuing an expired, cleared or already acknowledged token must not recreate
its old payload. The default store implements these rules and copies protobuf
messages so caller mutations cannot change queued work. An optional
`has_pending(node_id) -> bool` should include leased work, allowing the worker to
keep polling until it can recover an abandoned lease. Without this optional
method, the worker polls through a lease recovery window after reconnect or a
storage failure.

Adapters that track uncertain acknowledgements may return the affected emails
from `ack_users`; such adapters must also provide `request_refresh(node_id,
emails)` to durably request delivery of current application state. An optional
`refresh_claimed(node_id, claimed_users)` can similarly replace work delivered
across a full-sync fence with refresh markers. Without that hook, the worker
requeues those claims using the store's normal revision checks.

Cancellation limits how long this client sends a leased batch. It cannot undo a
request that a remote server has already accepted, or provide exactly-once
delivery. Process-local storage does not survive process termination; use a
durable shared store when that is required.

#### Resolve queued users from current application state

Applications can pass `user_sync_resolver=resolve_users` when constructing a
node. This optional async callback receives `(node_id, emails)` and returns one
current protobuf `User` for each requested email immediately before delivery.
The library does not access an application database. Return an explicit removal
payload, as appropriate for the node backend, for a deleted user. A missing email
or callback failure retries the batch without sending stale fallback data.

#### Coordinate full snapshots with background updates

Use a deferred loader when a full snapshot must coexist with queued updates:

```python
async def load_users():
    return await application.load_current_node_users(node.node_id)

await node.sync_users_from_source(load_users, timeout=30)
```

This acquires a shared full-sync fence, waits for already claimed deliveries to
finish, captures queued revisions, then calls the loader and sends the snapshot.
Only a successful snapshot retires captured revisions. Updates queued during
the snapshot remain available for subsequent delta delivery. Failures retain
queued work, and loss of the renewable fence aborts the operation. A competing
full sync receives `NodeAPIError(409)`.

The default in-memory store supports this operation across controllers in one
process. External stores must implement `SnapshotUserSyncStoreProtocol` with
atomic `begin_full_sync`, `renew_full_sync`, `end_full_sync`, `fence_state`,
`capture_queued` and `retire_captured` methods. A fence has an owner token, an
expiry and a monotonically increasing generation. Claims must be blocked while
it is active. Captures return pending revisions and the number of live claims;
retirement removes only matching revisions. Stale owners cannot renew or release
a replacement fence. All controllers sharing a store must honor this contract.
Stores implementing only `UserSyncStoreProtocol` retain the queue API, but the
new snapshot helper rejects them instead of silently using a local lock.

For custom orchestration, `full_sync_fence()`, `capture_queued_work()` and
`retire_queued_work(captured)` expose the same steps. Keep capture, the authoritative
read, transmission and retirement inside the fence; call the yielded hold's
`check()` before transmission and retirement. Prefer `sync_users_from_source()`
unless custom orchestration is necessary.

Existing direct `sync_users(...)` and `sync_users_chunked(...)` retain their
behavior. Their explicit `flush_pending=True` option clears queued work before
sending; use `sync_users_from_source()` when pending updates must survive a failed
full sync. `flush_pending_users()` remains an explicit queue discard operation.

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

A lifecycle adapter must atomically acquire/release leases and fence writes with the returned epoch. This prevents a cron status job or another worker from overwriting the result of a newer `start()`, `stop()`, or reconnect flow.

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

### 2. Direct User Sync

Use direct sync when you want explicit control in your flow.

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
- `sync_users(users, flush_pending=False, timeout=None)` (direct)
- `sync_users_chunked(users, chunk_size=100, flush_pending=False, timeout=None)` (direct streaming)

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
