import asyncio
from logging import DEBUG, Formatter, StreamHandler, getLogger

import PasarGuardNodeBridge as Bridge
from PasarGuardNodeBridge.common import service_pb2 as service

address = "172.27.158.135"
port = 2096
api_port = 2097
server_ca_file = "certs/ssl_cert.pem"
config_file = "config/xray.json"
api_key = "d04d8680-942d-4365-992f-9f482275691d"

with open(config_file, "r") as f:
    config = f.read()

with open(server_ca_file, "r") as f:
    server_ca_content = f.read()


async def main():
    # Create node with custom timeout configuration
    # - default_timeout: applies to all public API methods (start, stop, info, get_*, sync_*)
    # - internal_timeout: applies to internal gRPC/HTTP operations
    # These can be overridden per-call by passing timeout parameter to individual methods

    logger = getLogger("example-node")
    logger.setLevel(DEBUG)
    handler = StreamHandler()
    handler.setLevel(DEBUG)
    handler.setFormatter(Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    logger.addHandler(handler)

    node = Bridge.create_node(
        connection=Bridge.NodeType.grpc,
        address=address,
        port=port,
        api_port=api_port,
        server_ca=server_ca_content,
        api_key=api_key,
        extra={"id": 1},
        name="example-node",
        default_timeout=15,  # Custom default timeout for API calls (default: 10s)
        internal_timeout=20,
        logger=logger,  # Custom timeout for internal operations (default: 15s)
        # proxy="socks5://user:pass@127.0.0.1:1080",
    )

    # Start the node with custom timeout override (60s instead of instance default 15s)
    await node.start(config=config, backend_type=service.BackendType.XRAY, users=[], timeout=60)

    user = Bridge.create_user(
        email="jeff", proxies=Bridge.create_proxy(vmess_id="0d59268a-9847-4218-ae09-65308eb52e08"), inbounds=[]
    )

    wireguard_user = Bridge.create_user(
        email="wireguard@example.com",
        proxies=Bridge.create_proxy(
            wireguard_public_key="wireguard-public-key",
            wireguard_peer_ips=["10.10.0.2/32", "fd00::2/128"],
        ),
        inbounds=[],
    )

    await node.update_user(user)
    await node.update_user(wireguard_user)

    # Example: Call with instance default timeout (15s)
    try:
        await node.get_user_online_ip_list("does-not-exist@example.com")
    except Bridge.NodeAPIError as e:
        print(f"Expected error for non-existent user: {e.code}")

    # Example: Call with instance default timeout (15s)
    stats = await node.get_stats(service.StatType.Inbounds)
    print(f"Stats: {stats}")

    await asyncio.sleep(5)

    # Example: Override timeout for this specific call (5s instead of instance default 15s)
    stats = await node.get_system_stats(timeout=5)
    print(f"System stats: {stats}")

    # Stream logs on-demand using context manager with real-time error detection
    print("\n--- Streaming logs (real-time error detection) ---")
    try:
        async with node.stream_logs(max_queue_size=100) as log_queue:
            # Read logs in a loop
            for _ in range(10):  # Try to get up to 10 log messages
                try:
                    item = await asyncio.wait_for(log_queue.get(), timeout=0.5)

                    # IMPORTANT: Check if we received an error instead of a log
                    if isinstance(item, Bridge.NodeAPIError):
                        # Error occurred during streaming - raise it immediately
                        raise item

                    # It's a normal log message
                    print(f"LOG: {item}")

                except asyncio.TimeoutError:
                    print("No more logs received within timeout")
                    break

    except Bridge.NodeAPIError as e:
        # Only print error if it's not an empty cleanup error
        if e.code != 0 or e.detail:
            print("\n!!! Log stream error detected !!!")
            print(f"Error code: {e.code}")
            print(f"Error detail: {e.detail}")
            print("In production, would attempt to reconnect and resume streaming...")

    await node.stop()


asyncio.run(main())
