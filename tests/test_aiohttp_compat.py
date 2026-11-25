import unittest

from PasarGuardNodeBridge.aiohttp_compat import BufferedResponse, BufferedStatusError


class BufferedResponseTests(unittest.TestCase):
    def test_status_validation(self):
        for status in (100, 199, 200, 204, 299, *range(300, 400), 400, 401, 404, 500, 503):
            with self.subTest(status=status):
                response = BufferedResponse(status, b"", {}, "https://node.example/")
                if status >= 300:
                    with self.assertRaises(BufferedStatusError) as caught:
                        response.raise_for_status()
                    self.assertIs(caught.exception.response, response)
                else:
                    response.raise_for_status()
