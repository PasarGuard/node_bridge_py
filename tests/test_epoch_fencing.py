import asyncio
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from grpclib.const import Status
from grpclib.exceptions import GRPCError

from PasarGuardNodeBridge.common import service_pb2 as service
from PasarGuardNodeBridge.controller import Controller, Health, NodeAPIError
from PasarGuardNodeBridge.grpclib import Node as GrpcNode
from PasarGuardNodeBridge.rest import Node as RestNode
from PasarGuardNodeBridge.storage import InMemoryUserSyncStore


def _configured_node(node_type, store: InMemoryUserSyncStore):
    node = node_type.__new__(node_type)
    node.name = "node-1"
    node.node_id = "node-1"
    node.worker_id = "worker-1"
    node.logger = logging.getLogger("test-epoch-fencing")
    node._user_sync_store = store
    node._sync_lease_seconds = 30
    node._default_timeout = 1
    node._internal_timeout = 1
    node._node_lock = asyncio.Lock()
    node._user_sync_epoch_supported = False
    node._user_sync_epoch_capability_probed = False
    node._user_sync_epoch_handshake_lock = asyncio.Lock()
    node._user_sync_connection_generation = 0
    node.get_health = AsyncMock(return_value=Health.HEALTHY)
    node._acquire_lifecycle_lease = AsyncMock(return_value=None)
    node._release_lifecycle_lease = AsyncMock()
    node.connect = AsyncMock()
    node.disconnect = AsyncMock()
    return node


class EpochTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_stale_epoch_retries_once_without_poison(self):
        for node_type in (RestNode, GrpcNode):
            with self.subTest(node_type=node_type.__module__):
                store = InMemoryUserSyncStore()
                node = _configured_node(node_type, store)
                node._user_sync_epoch_supported = True
                node._user_sync_epoch_capability_probed = True
                observed = []

                async def transport(request):
                    observed.append(request.user_sync_epoch)
                    if len(observed) == 1:
                        if node_type is GrpcNode:
                            raise GRPCError(Status.FAILED_PRECONDITION, "stale user sync epoch")
                        raise NodeAPIError(412, "stale user sync epoch")
                    return service.BaseInfoResponse(
                        started=True,
                        node_version="0.4.0",
                        core_version="1.0.0",
                        user_sync_epoch_supported=True,
                        user_sync_epoch=request.user_sync_epoch,
                    )

                if node_type is RestNode:

                    async def make_request(**kwargs):
                        return await transport(kwargs["proto_message"])

                    node._make_request = make_request
                else:
                    node._client = SimpleNamespace(Start=object())

                    async def grpc_request(**kwargs):
                        return await transport(kwargs["request"])

                    node._handle_grpc_request = grpc_request

                result = await node.start("{}", service.BackendType.XRAY, [service.User(email="42")])

                self.assertTrue(result.started)
                self.assertEqual(observed, [1, 2])
                self.assertEqual(store._user_sync_leases, {})

    async def test_snapshot_stale_epoch_retry_is_bounded_and_does_not_poison(self):
        for node_type in (RestNode, GrpcNode):
            for method_name in ("sync_users", "reconcile_users"):
                with self.subTest(node_type=node_type.__module__, method=method_name):
                    store = InMemoryUserSyncStore()
                    node = _configured_node(node_type, store)
                    node._user_sync_epoch_supported = True
                    node._user_sync_epoch_capability_probed = True
                    observed = []

                    async def always_stale(request):
                        observed.append(request.user_sync_epoch)
                        if node_type is GrpcNode:
                            raise GRPCError(Status.FAILED_PRECONDITION, "stale user sync epoch")
                        raise NodeAPIError(412, "stale user sync epoch")

                    if node_type is RestNode:

                        async def make_request(**kwargs):
                            return await always_stale(kwargs["proto_message"])

                        node._make_request = make_request
                    else:
                        node._client = SimpleNamespace(SyncUsers=object())

                        async def grpc_request(**kwargs):
                            return await always_stale(kwargs["request"])

                        node._handle_grpc_request = grpc_request

                    with self.assertRaises((NodeAPIError, GRPCError)):
                        await getattr(node, method_name)([service.User(email="42")])

                    self.assertEqual(observed, [1, 2])
                    self.assertEqual(store._user_sync_leases, {})

    async def test_reversed_direct_delivery_retries_once_with_newer_epoch(self):
        for node_type in (RestNode, GrpcNode):
            with self.subTest(node_type=node_type.__module__):
                store = InMemoryUserSyncStore()
                first = _configured_node(node_type, store)
                second = _configured_node(node_type, store)
                first.worker_id = "worker-1"
                second.worker_id = "worker-2"
                for node in (first, second):
                    node._user_sync_epoch_supported = True
                    node._user_sync_epoch_capability_probed = True

                first_entered = asyncio.Event()
                release_first = asyncio.Event()
                applied_epoch = 0
                observed = {"worker-1": [], "worker-2": []}

                def transport_for(node):
                    async def transport(_users, _chunk_size, _timeout, user_sync_epoch):
                        nonlocal applied_epoch
                        observed[node.worker_id].append(user_sync_epoch)
                        if node.worker_id == "worker-1" and len(observed[node.worker_id]) == 1:
                            first_entered.set()
                            await release_first.wait()
                        if user_sync_epoch < applied_epoch:
                            if node_type is GrpcNode:
                                raise GRPCError(Status.FAILED_PRECONDITION, "stale user sync epoch")
                            raise NodeAPIError(412, "stale user sync epoch")
                        applied_epoch = user_sync_epoch

                    return transport

                first._sync_users_chunked_transport = transport_for(first)
                second._sync_users_chunked_transport = transport_for(second)
                users = [service.User(email="42")]

                first_sync = asyncio.create_task(first.sync_users_chunked(users))
                await asyncio.wait_for(first_entered.wait(), timeout=1)
                self.assertEqual(await second.sync_users_chunked(users), [])
                release_first.set()
                self.assertEqual(await asyncio.wait_for(first_sync, timeout=1), [])

                self.assertEqual(observed["worker-1"], [1, 3])
                self.assertEqual(observed["worker-2"], [2])
                self.assertEqual(applied_epoch, 3)
                self.assertEqual(store._user_sync_leases, {})

    async def test_stale_direct_delivery_retry_is_bounded_and_does_not_poison(self):
        for node_type in (RestNode, GrpcNode):
            with self.subTest(node_type=node_type.__module__):
                store = InMemoryUserSyncStore()
                node = _configured_node(node_type, store)
                node._user_sync_epoch_supported = True
                node._user_sync_epoch_capability_probed = True
                observed = []

                async def always_stale(_users, _chunk_size, _timeout, user_sync_epoch):
                    observed.append(user_sync_epoch)
                    if node_type is GrpcNode:
                        raise GRPCError(Status.FAILED_PRECONDITION, "stale user sync epoch")
                    raise NodeAPIError(412, "stale user sync epoch")

                node._sync_users_chunked_transport = always_stale
                users = [service.User(email="42")]

                self.assertEqual(await node.sync_users_chunked(users), users)
                self.assertEqual(observed, [1, 2])
                self.assertEqual(store._user_sync_leases, {})

    async def test_rest_start_cancellation_during_connect_is_not_converted(self):
        store = InMemoryUserSyncStore()
        node = _configured_node(RestNode, store)
        node._user_sync_epoch_supported = True
        node._user_sync_epoch_capability_probed = True
        connect_entered = asyncio.Event()

        async def transport(**_kwargs):
            return service.BaseInfoResponse(
                started=True,
                node_version="0.4.0",
                core_version="1.0.0",
                user_sync_epoch_supported=True,
                user_sync_epoch=1,
            )

        async def blocking_connect(_node_version, _core_version):
            connect_entered.set()
            await asyncio.Event().wait()

        node._make_request = transport
        node.connect = blocking_connect
        start = asyncio.create_task(node.start("{}", service.BackendType.XRAY, [service.User(email="42")]))
        await asyncio.wait_for(connect_entered.wait(), timeout=1)
        start.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await start
        node.disconnect.assert_awaited_once()
        node._release_lifecycle_lease.assert_awaited_once()
        self.assertEqual(store._user_sync_leases, {})

    async def test_fresh_reconcile_start_probes_capability_and_advances_floor(self):
        for node_type in (RestNode, GrpcNode):
            with self.subTest(node_type=node_type.__module__):
                store = InMemoryUserSyncStore()
                node = _configured_node(node_type, store)
                captured: list[int] = []

                async def response_for(request):
                    if request is None:
                        return service.BaseInfoResponse(
                            started=False,
                            user_sync_epoch_supported=True,
                            user_sync_epoch=40,
                        )
                    captured.append(request.user_sync_epoch)
                    return service.BaseInfoResponse(
                        started=True,
                        node_version="0.4.0",
                        core_version="1.0.0",
                        user_sync_epoch_supported=True,
                        user_sync_epoch=request.user_sync_epoch,
                    )

                if node_type is RestNode:

                    async def make_request(**kwargs):
                        return await response_for(kwargs.get("proto_message"))

                    node._make_request = make_request
                else:
                    info_method = object()
                    start_method = object()
                    node._client = SimpleNamespace(GetBaseInfo=info_method, Start=start_method)

                    async def grpc_request(**kwargs):
                        request = None if kwargs["method"] is info_method else kwargs["request"]
                        return await response_for(request)

                    node._handle_grpc_request = grpc_request

                result = await node.start(
                    config="{}",
                    backend_type=service.BackendType.XRAY,
                    users=[service.User(email="42")],
                    reconcile_user_sync=True,
                )

                self.assertTrue(result.started)
                self.assertEqual(captured, [41])

    async def test_disconnect_invalidates_delayed_capability_response(self):
        class BlockingAdvanceStore(InMemoryUserSyncStore):
            def __init__(self):
                super().__init__()
                self.entered = asyncio.Event()
                self.release = asyncio.Event()

            async def advance_user_sync_epoch(self, node_id: str, minimum_epoch: int) -> None:
                self.entered.set()
                await self.release.wait()
                await super().advance_user_sync_epoch(node_id, minimum_epoch)

        store = BlockingAdvanceStore()
        controller = object.__new__(Controller)
        controller.node_id = "node-1"
        controller._user_sync_store = store
        controller._user_sync_epoch_supported = False
        controller._user_sync_epoch_capability_probed = False
        controller._user_sync_epoch_handshake_lock = asyncio.Lock()
        controller._user_sync_connection_generation = 0
        controller._shutdown_event = asyncio.Event()
        controller._task_lock = asyncio.Lock()
        controller._cleanup_tasks = AsyncMock()
        controller._cleanup_sync_worker = AsyncMock()
        controller._health_lock = asyncio.Lock()
        controller._version_lock = asyncio.Lock()
        controller._node_version = "0.4.0"
        controller._core_version = "1.0.0"
        controller._health = Health.HEALTHY

        observation = asyncio.create_task(
            controller._observe_user_sync_epoch_capability(
                service.BaseInfoResponse(
                    user_sync_epoch_supported=True,
                    user_sync_epoch=10,
                ),
                expected_generation=0,
            )
        )
        await store.entered.wait()
        disconnect = asyncio.create_task(controller.disconnect())
        await asyncio.sleep(0)
        store.release.set()
        await observation
        await disconnect

        self.assertFalse(controller._user_sync_epoch_supported)
        self.assertFalse(controller._user_sync_epoch_capability_probed)
        self.assertEqual(controller._user_sync_connection_generation, 1)

    async def test_probe_cannot_reapply_response_from_disconnected_generation(self):
        controller = object.__new__(Controller)
        controller.node_id = "node-1"
        controller._user_sync_store = InMemoryUserSyncStore()
        controller._user_sync_epoch_supported = False
        controller._user_sync_epoch_capability_probed = False
        controller._user_sync_epoch_handshake_lock = asyncio.Lock()
        controller._user_sync_connection_generation = 0
        controller._shutdown_event = asyncio.Event()
        controller._task_lock = asyncio.Lock()
        controller._cleanup_tasks = AsyncMock()
        controller._cleanup_sync_worker = AsyncMock()
        controller._health_lock = asyncio.Lock()
        controller._version_lock = asyncio.Lock()
        controller._node_version = "0.4.0"
        controller._core_version = "1.0.0"
        controller._health = Health.HEALTHY
        entered = asyncio.Event()
        release = asyncio.Event()

        async def delayed_info():
            entered.set()
            await release.wait()
            return service.BaseInfoResponse(
                user_sync_epoch_supported=True,
                user_sync_epoch=10,
            )

        controller.info = delayed_info
        probe = asyncio.create_task(controller._ensure_user_sync_epoch_support())
        await entered.wait()
        await controller.disconnect()
        release.set()
        with self.assertRaises(NodeAPIError) as error:
            await probe

        self.assertEqual(error.exception.code, 426)
        self.assertFalse(controller._user_sync_epoch_supported)
        self.assertFalse(controller._user_sync_epoch_capability_probed)


if __name__ == "__main__":
    unittest.main()
