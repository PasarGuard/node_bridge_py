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
Version: 0.2.3
"""

__version__ = "0.2.3"
__author__ = "PasarGuard"


from enum import Enum

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
    **kwargs,
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
        **kwargs: Additional optional arguments:
            - name (str): Node instance name for logging. Defaults to "default".
            - extra (dict): Optional dictionary to pass custom metadata or configuration. Defaults to {}.
            - logger (logging.Logger): Custom logger instance. If None, a default logger is created.
            - default_timeout (int): Default timeout in seconds for public API methods. Defaults to 10.
            - internal_timeout (int): Default timeout in seconds for internal operations. Defaults to 15.

    Returns:
        PasarGuardNode: An initialized node instance ready for API operations.

    Raises:
        ValueError: If the provided connection type is invalid.
        NodeAPIError: If the node connection or initialization fails.

    Examples:
        >>> # Basic usage with defaults
        >>> node = create_node(
        ...     connection=NodeType.grpc,
        ...     address="172.27.158.135",
        ...     port=2096,
        ...     server_ca=server_ca_content,
        ...     api_key=api_key,
        ... )
        >>>
        >>> # With custom timeouts
        >>> node = create_node(
        ...     connection=NodeType.grpc,
        ...     address="172.27.158.135",
        ...     port=2096,
        ...     server_ca=server_ca_content,
        ...     api_key=api_key,
        ...     default_timeout=30,
        ...     internal_timeout=20,
        ... )

    Note:
        - SSL certificate values should be passed as strings, not file paths.
        - Use `extra` to inject any environment-specific settings or context.
        - Timeout values can be overridden per-call in individual API methods.
    """

    if connection is NodeType.grpc:
        return GrpcNode(
            address=address,
            port=port,
            server_ca=server_ca,
            api_key=api_key,
            **kwargs,
        )

    elif connection is NodeType.rest:
        return RestNode(
            address=address,
            port=port,
            server_ca=server_ca,
            api_key=api_key,
            **kwargs,
        )

    else:
        raise ValueError("invalid backend type")


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
