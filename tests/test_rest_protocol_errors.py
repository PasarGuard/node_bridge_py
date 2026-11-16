"""A REST node behind something that does not answer with HTTP/1.1 gets a readable error, not aiohttp's parser text."""

import asyncio
import logging
import unittest
from unittest.mock import patch

from aiohttp import http_parser

from PasarGuardNodeBridge import NodeAPIError
from PasarGuardNodeBridge.aiohttp_compat import LazyClientSession, make_timeout
from PasarGuardNodeBridge.common import service_pb2 as service
from PasarGuardNodeBridge.rest import Node as RestNode
from PasarGuardNodeBridge.storage import InMemoryUserSyncStore

# What a server sends first on an HTTP/2-only port: a SETTINGS frame, here followed by GOAWAY
HTTP2_FRAMES = bytes.fromhex("000006040000000000000300000064") + bytes.fromhex("000008070000000000000000000000000001")
# The start of a TLS record (a handshake, as a TLS-only port would send), received over plain HTTP
TLS_RECORD = bytes.fromhex("160303007a020000760303")
# aiohttp's C parser rejects such bytes at once; the pure-Python one waits for a header block and sees a closed connection
C_PARSER = http_parser.HttpResponseParser.__module__ == "aiohttp._http_parser"


class RestProtocolErrorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.answer = b""
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        with patch("ssl.SSLContext.load_verify_locations"):
            self.node = RestNode(
                address="127.0.0.1",
                port=port,
                api_port=1,
                server_ca="test",
                api_key="00000000-0000-0000-0000-000000000000",
                node_id="protocol",
                user_sync_store=InMemoryUserSyncStore(),
                logger=logging.getLogger("rest-protocol-test"),
            )
        await self.node._client.close()
        self.node._client = LazyClientSession(
            ssl_context=None,
            headers={"Content-Type": "application/x-protobuf"},
            base_url=f"http://127.0.0.1:{port}/",
            timeout=make_timeout(None),
        )

    async def asyncTearDown(self):
        await self.node._client.close()
        await self.node._json_client.close()
        self.server.close()
        await self.server.wait_closed()

    async def handle(self, reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        if self.answer:
            writer.write(self.answer)
            await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def request_error(self) -> NodeAPIError:
        with self.assertRaises(NodeAPIError) as caught:
            await self.node.info(timeout=5)
        return caught.exception

    @unittest.skipUnless(C_PARSER, "needs aiohttp's C HTTP parser")
    async def test_http2_answer_is_reported_as_protocol_error(self):
        self.answer = HTTP2_FRAMES
        error = await self.request_error()
        self.assertEqual(error.code, -2)
        self.assertIn("did not answer with HTTP/1.1 (got HTTP/2)", error.detail)
        self.assertIn("check the reverse proxy and TLS settings", error.detail)
        self.assertNotIn("Expected HTTP/", error.detail)

    async def test_http2_status_line_is_reported_as_protocol_error(self):
        self.answer = b"HTTP/2 200\r\ncontent-length: 0\r\n\r\n"
        error = await self.request_error()
        self.assertEqual(error.code, -2)
        self.assertIn("(got HTTP/2)", error.detail)

    async def test_http3_status_line_is_labelled_http3(self):
        self.answer = b"HTTP/3 200\r\ncontent-length: 0\r\n\r\n"
        error = await self.request_error()
        self.assertEqual(error.code, -2)
        self.assertIn("(got HTTP/3)", error.detail)

    async def test_version_prefix_alone_is_not_http2(self):
        self.answer = b"HTTP/200 OK\r\ncontent-length: 0\r\n\r\n"
        error = await self.request_error()
        self.assertEqual(error.code, -2)
        self.assertIn("did not answer with HTTP/1.1 (got b'HTTP/200 OK')", error.detail)

    async def test_client_preface_line_with_junk_is_quoted(self):
        self.answer = b"PRI * HTTP/2.0 junk\r\n\r\n"
        error = await self.request_error()
        self.assertEqual(error.code, -2)
        self.assertIn("(got b'PRI * HTTP/2.0 junk')", error.detail)

    async def test_client_preface_is_http2(self):
        self.answer = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
        error = await self.request_error()
        self.assertEqual(error.code, -2)
        self.assertIn("(got HTTP/2)", error.detail)

    @unittest.skipUnless(C_PARSER, "needs aiohttp's C HTTP parser")
    async def test_tls_answer_is_reported_as_protocol_error(self):
        self.answer = TLS_RECORD
        error = await self.request_error()
        self.assertEqual(error.code, -2)
        self.assertIn("(got TLS data)", error.detail)

    async def test_other_bytes_are_quoted(self):
        self.answer = b"SSH-2.0-OpenSSH_9.6\r\n\r\n"
        error = await self.request_error()
        self.assertEqual(error.code, -2)
        self.assertIn("(got b'SSH-2.0-OpenSSH_9.6')", error.detail)

    async def test_empty_close_is_still_reported_as_closed_connection(self):
        error = await self.request_error()
        self.assertEqual(error.code, 500)
        self.assertIn("Server closed connection", error.detail)

    async def test_malformed_http11_status_line_keeps_400(self):
        for answer in (b"HTTP/1.1 abc OK\r\n\r\n", b"HTTP/1.1 2000 OK\r\n\r\n", b"HTTP/1.0 20 OK\r\n\r\n"):
            with self.subTest(answer=answer):
                self.answer = answer
                error = await self.request_error()
                self.assertEqual(error.code, 400)
                self.assertTrue(error.detail.startswith("HTTP error: "), error.detail)

    @unittest.skipUnless(C_PARSER, "needs aiohttp's C HTTP parser")
    async def test_frame_type_byte_alone_is_not_http2(self):
        # a SETTINGS (0x04) or GOAWAY (0x07) type byte at offset 3, but a non-zero stream id or a bad length
        for answer in (
            bytes.fromhex("000006040000000001") + b"abcdef",
            bytes.fromhex("000005040000000000") + b"abcde",
            bytes.fromhex("000008070000000001") + b"abcdefgh",
            bytes.fromhex("000004070000000000") + b"abcd",
        ):
            with self.subTest(answer=answer):
                self.answer = answer
                error = await self.request_error()
                self.assertEqual(error.code, -2)
                self.assertIn(f"(got {answer!r})", error.detail)

    async def test_http_error_status_is_unchanged(self):
        self.answer = b"HTTP/1.1 400 Bad Request\r\ncontent-length: 8\r\nconnection: close\r\n\r\nbad body"
        error = await self.request_error()
        self.assertEqual(error.code, 400)
        self.assertEqual(error.detail, "HTTP error: bad body")

    async def test_chunked_sync_reports_protocol_error(self):
        self.answer = b"HTTP/2 200\r\ncontent-length: 0\r\n\r\n"
        users = [service.User(email=f"user-{index}") for index in range(3)]
        failed = await self.node.sync_users_chunked(users, chunk_size=2, timeout=5)
        self.assertEqual(list(failed), users)
        self.assertFalse(failed.rejected)
        self.assertIn("Protocol error: Node did not answer with HTTP/1.1 (got HTTP/2)", failed.detail)

    async def test_log_stream_open_reports_protocol_error(self):
        self.answer = b"HTTP/2 200\r\ncontent-type: text/plain\r\n\r\n"
        with self.assertRaises(NodeAPIError) as caught:
            async with self.node.stream_logs():
                pass
        self.assertEqual(caught.exception.code, -2)
        self.assertIn("(got HTTP/2)", caught.exception.detail)


if __name__ == "__main__":
    unittest.main()
