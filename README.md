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

```python
await node.update_user(user)

more_users = [user1, user2, user3]
await node.update_users(more_users)
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
- `get_user_online_stats(email, timeout=None)`
- `get_user_online_ip_list(email, timeout=None)`

### User Sync

- `update_user(user)` (queued/background)
- `update_users(users)` (queued/background)
- `sync_users(users, flush_pending=False, timeout=None)` (direct)
- `sync_users_chunked(users, chunk_size=100, flush_pending=False, timeout=None)` (direct streaming)

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
