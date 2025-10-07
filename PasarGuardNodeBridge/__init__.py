"""
PasarGuard Node Bridge

A Python library for interfacing with PasarGuard nodes via gRPC or REST protocols.
This library abstracts communication with PasarGuard nodes, allowing for
user management, proxy configuration, and health monitoring through a unified interface.

Features:
- Support for both gRPC and REST connections
- SSL/TLS secure communication
- High-level API for common node operations
- Extensible with custom metadata via the `extra` argument

Author: PasarGuard
Version: 0.0.60
"""

__version__ = "0.0.60"
__author__ = "PasarGuard"


from enum import Enum
import logging
from typing import Optional

from PasarGuardNodeBridge.abstract_node import PasarGuardNode
from PasarGuardNodeBridge.grpclib import Node as GrpcNode
from PasarGuardNodeBridge.rest import Node as RestNode
from PasarGuardNodeBridge.controller import NodeAPIError, Health
from PasarGuardNodeBridge.utils import create_user, create_proxy


class NodeType(str, Enum):
    grpc = "grpc"
    rest = "rest"


def create_node(
    connection: NodeType,
    address: str,
    port: int,
    server_ca: str,
    api_key: str,
    name: str = "default",
    max_logs: int = 1000,
    extra: dict = {},
    logger: Optional[logging.Logger] = None,
) -> PasarGuardNode:
    """
    Create and initialize a PasarGuard node instance using the specified connection type.

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
        PasarGuardNode: An initialized node instance ready for API operations.

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
            name=name,
            extra=extra,
            max_logs=max_logs,
            logger=logger,
        )

    elif connection is NodeType.rest:
        node = RestNode(
            address=address,
            port=port,
            server_ca=server_ca,
            api_key=api_key,
            name=name,
            extra=extra,
            max_logs=max_logs,
            logger=logger,
        )

    else:
        raise ValueError("invalid backend type")

    return node


__all__ = [
    "PasarGuardNode",
    "NodeType",
    "Node",
    "NodeAPIError",
    "Health",
    "create_user",
    "create_proxy",
    "create_node",
]
