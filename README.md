# node_bridge_py
Library to connect and use https://github.com/PasarGuard/node

# Usage
```shell
pip install pasarguard-node-bridge
```
Library is fully async for both `gRPC` and `REST API` connection.

# Example
```python
import PasarGuardNodeBridge as Bridge
# or
import PasarGuardNodeBridge as PGNB
```

## Creating Node
```python
node = Bridge.create_node(
    connection=Bridge.NodeType.GRPC, # GRPC or REST
    address=address, # Node ip address or domain
    port=port, 
    client_cert=client_cert_content, # client side ssl certificate as string
    client_key=client_key_content, # client side ssl key as string
    server_ca=server_ca_content, # server side ssl key as string
    extra={}, # a dictionary to hold node data on production, optional, default: None
    )
```

## Proto Structure
If you need to have access proto structure you can use:
```python
from PasarGuardNodeBridge.common import service_pb2 as service
```

## Create User
```python
user = Bridge.create_user(
    email="jeff", 
    proxies=Bridge.create_proxy(
        vmess_id="0d59268a-9847-4218-ae09-65308eb52e08", # UUID converted to str
        vless_id="0d59268a-9847-4218-ae09-65308eb52e08", # UUID converted to str
        vless_flow="",              # Valid vless flow if is set for user (str)
        trojan_password="",         # Trojan password (str)
        shadowsocks_password="",    # Shadowsocks password (str)
        shadowsocks_method="",      # Valid shadowsocks method supported by backend
        ), 
    inbounds=[]                     # List of outbound tag, List[str]
    )
```

## Methods
Before use any method you need to call start method and connect to node unless you will face `NodeAPIError` for every method
```python
await node.start(
    config=config,  # backend config as string 
    backend_type=0, # backend type , XRAY = 0
    users=[],       # list of users you want to add to this node, will be recheck in node with config
    timeout=20,
    )
```

### Get User Stats
```python
stats = await node.get_user_stats(
    email="noreply@donate.pasarguard.org",
    reset=True,
    timeout=10,
    )
```

### Health
Return a `Bridge.Health`
```python
health = await node.get_health()
```

### Logs
Return a `asyncio.Queue[str]` 
```python
logs = await node.get_logs()
```
