import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from PasarGuardNodeBridge import NodeType, create_node
from PasarGuardNodeBridge.abstract_node import PasarGuardNode
from PasarGuardNodeBridge.controller import NodeAPIError


class ConstructorCompatibilityTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.api_key = str(uuid4())
        self.ssl_context = MagicMock()
        self.ssl_patch = patch(
            "PasarGuardNodeBridge.controller.ssl.create_default_context",
            return_value=self.ssl_context,
        )
        self.ssl_patch.start()
        self.addCleanup(self.ssl_patch.stop)

    async def test_extra_attribute_remains_available(self) -> None:
        node = create_node(
            connection=NodeType.rest,
            address="127.0.0.1",
            port=2096,
            api_port=2097,
            server_ca="test-ca.pem",
            api_key=self.api_key,
            extra={"region": "eu"},
        )

        self.assertEqual(node.extra, {"region": "eu"})
        node.extra = {"region": "us"}
        self.assertEqual(await node.get_extra(), {"region": "us"})

    async def test_legacy_subclass_without_reconcile_method_remains_instantiable(self) -> None:
        async def legacy_method(*_args, **_kwargs):
            return None

        legacy_methods = {
            name: legacy_method for name in PasarGuardNode.__abstractmethods__ if name != "reconcile_users"
        }
        legacy_type = type("LegacyNode", (PasarGuardNode,), legacy_methods)
        node = legacy_type.__new__(legacy_type)

        with self.assertRaises(NodeAPIError) as error:
            await node.reconcile_users([])

        self.assertEqual(error.exception.code, 501)
if __name__ == "__main__":
    unittest.main()
