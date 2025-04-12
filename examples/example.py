import asyncio

import GozargahNodeBridge as Bridge

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
        max_logs=100,
        extra={"id": 1},
    )

    await node.start(config=config, backend_type=0, users=[], timeout=20)

    user = Bridge.create_user(
        email="jeff", proxies=Bridge.create_proxy(vmess_id="0d59268a-9847-4218-ae09-65308eb52e08"), inbounds=[]
    )

    await node.update_user(user)

    await asyncio.sleep(5)

    stats = await node.get_outbounds_stats()
    print(stats)

    logs = await node.get_logs()

    print(await logs.get())

    await node.stop()


asyncio.run(main())
