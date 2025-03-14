generate_grpc_code:
	uv run python3 -m grpc_tools.protoc \
		-I. \
		--python_out=. \
		--pyi_out=. \
		--grpclib_python_out=. \
		GozargahNodeBridge/common/service.proto

generate_server_cert:
	openssl req -x509 -newkey rsa:4096 -keyout ./certs/ssl_key.pem \
	-out ./certs/ssl_cert.pem -days 36500 -nodes -subj "/CN=Gozargah"

generate_client_cert:
	openssl req -x509 -newkey rsa:4096 -keyout ./certs/ssl_client_key.pem \
 	-out ./certs/ssl_client_cert.pem -days 36500 -nodes -subj "/CN=Gozargah"

install_uv:
	curl -LsSf https://astral.sh/uv/install.sh | sh

format:
	ruff format .
