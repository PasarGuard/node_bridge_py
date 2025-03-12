import asyncio

import GozargahNodeBridge as Bridge

address = "172.27.158.135"
port = 62050
client_cert_file = "certs/ssl_client_cert.pem"
client_key_file = "certs/ssl_client_key.pem"
server_ca_file = "certs/ssl_cert.pem"
config_file = "config/xray.json"

with open(config_file, "r") as f:
    config = f.read()

with open(client_cert_file, "r") as f:
    client_cert_content = f.read()

with open(client_key_file, "r") as f:
    client_key_content = f.read()

with open(server_ca_file, "r") as f:
    server_ca_content = f.read()


async def main():
    node = Bridge.create_node(
        connection=Bridge.NodeType.grpc,
        address=address,
        port=port,
        client_cert=client_cert_content,
        client_key=client_key_content,
        server_ca=server_ca_content,
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
