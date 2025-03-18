"""
Gozargah Node Bridge
A Python library for connecting to Gozargah nodes.
"""

__version__ = "0.0.26"
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
    client_cert: str,
    client_key: str,
    server_ca: str,
    max_logs: int = 1000,
    extra: dict = {},
) -> GozargahNode:
    """
    Create and initialize a node instance.

    Args:
         connection (NodeType): The type of connection to use for the node.
             Must be `NodeType.GRPC` or `NodeType.REST`.
         address (str): The address of the node.
             This must be a valid IPv4, IPv6 or valid domain.
         port (int): The port number for the node connection.
         client_cert (str): The SSL certificate as a string (not a file path).
         client_key (str): The SSL private key as a string (not a file path).
         server_ca (str): The server SSL certificate as a string (not a file path).
         extra (dict): extra data you need in production.

    Returns:
        Node | None: A `Node` instance if successfully created; otherwise, `None`.

    Raises:
        NodeAPIError: If the `connection` failed.

    Note:
        - The `address` must be a valid IP address or domain.
        - The `client_cert`, `client_key` and `server_ca` should be the content of the SSL certificate
          and private key as strings, not file paths.
    """

    if connection is NodeType.grpc:
        node = GrpcNode(
            address=address,
            port=port,
            client_cert=client_cert,
            client_key=client_key,
            server_ca=server_ca,
            extra=extra,
            max_logs=max_logs,
        )

    elif connection is NodeType.rest:
        node = RestNode(
            address=address,
            port=port,
            client_cert=client_cert,
            client_key=client_key,
            server_ca=server_ca,
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
