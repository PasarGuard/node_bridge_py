"""
Gozargah Node Bridge

A Python library for interfacing with Gozargah nodes via gRPC or REST protocols.
This library abstracts communication with gozargah-node, allowing for
user management, proxy configuration, and health monitoring through a unified interface.

Features:
- Support for both gRPC and REST connections
- SSL/TLS secure communication
- High-level API for common node operations
- Extensible with custom metadata via the `extra` argument

Author: M03ED
Version: 0.0.33
"""

__version__ = "0.0.33"
__author__ = "M03ED"


from enum import Enum

from GozargahNodeBridge.abstract_node import GozargahNode
from GozargahNodeBridge.grpclib import Node as GrpcNode
from GozargahNodeBridge.rest import Node as RestNode
from GozargahNodeBridge.controller import NodeAPIError, Health
from GozargahNodeBridge.utils import create_user, create_proxy


class NodeType(str, Enum):
    grpc = "grpc"
    rest = "rest"


def create_node(
    connection: NodeType,
    address: str,
    port: int,
    server_ca: str,
    api_key: str,
    max_logs: int = 1000,
    extra: dict = {},
) -> GozargahNode:
    """
    Create and initialize a Gozargah node instance using the specified connection type.

    This function abstracts the creation of either a gRPC-based or REST-based node,
    handling the underlying setup and returning a ready-to-use node object.

    Args:
        connection (NodeType): Type of node connection. Must be `NodeType.grpc` or `NodeType.rest`.
        address (str): IP address or domain name of the node.
        port (int): Port number used to connect to the node.
        server_ca (str): The server's SSL certificate as a string (PEM format).
        api_key (str): API key used for authentication with the node.
        max_logs (int, optional): Maximum number of logs to retain. Defaults to 1000.
        extra (dict, optional): Optional dictionary to pass custom metadata or configuration.

    Returns:
        GozargahNode: An initialized node instance ready for API operations.

    Raises:
        ValueError: If the provided connection type is invalid.
        NodeAPIError: If the node connection or initialization fails.

    Note:
        - SSL certificate values should be passed as strings, not file paths.
        - Use `extra` to inject any environment-specific settings or context.
    """

    if connection is NodeType.grpc:
        node = GrpcNode(
            address=address,
            port=port,
            server_ca=server_ca,
            api_key=api_key,
            extra=extra,
            max_logs=max_logs,
        )

    elif connection is NodeType.rest:
        node = RestNode(
            address=address,
            port=port,
            server_ca=server_ca,
            api_key=api_key,
            extra=extra,
            max_logs=max_logs,
        )

    else:
        raise ValueError("invalid backend type")

    return node


__all__ = [
    "NodeType",
    "Node",
    "NodeAPIError",
    "Health",
    "create_user",
    "create_proxy",
    "create_node",
]
