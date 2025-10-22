import asyncio

import PasarGuardNodeBridge as Bridge

address = "172.27.158.135"
port = 2096
server_ca_file = "certs/ssl_cert.pem"
config_file = "config/xray.json"
api_key = "d04d8680-942d-4365-992f-9f482275691d"

with open(config_file, "r") as f:
    config = f.read()

with open(server_ca_file, "r") as f:
    server_ca_content = f.read()


async def main():
    node = Bridge.create_node(
        connection=Bridge.NodeType.grpc,
        address=address,
        port=port,
        server_ca=server_ca_content,
        api_key=api_key,
        extra={"id": 1},
    )

    await node.start(config=config, backend_type=0, users=[], timeout=20)

    user = Bridge.create_user(
        email="jeff", proxies=Bridge.create_proxy(vmess_id="0d59268a-9847-4218-ae09-65308eb52e08"), inbounds=[]
    )

    await node.update_user(user)
    try:
        await node.get_user_online_ip_list("does-not-exist@example.com")
    except Bridge.NodeAPIError as e:
        print(e.code)

    stats = await node.get_stats(0)
    print(stats)

    await asyncio.sleep(5)

    stats = await node.get_system_stats()
    print(stats)

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
