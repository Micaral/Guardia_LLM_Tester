import tempfile
import time
import unittest
from pathlib import Path

from guardia_tester.api import CurlKarasenaClient, RequestTemplateError


SAMPLE_CURL = """curl 'https://example.test/api/comprobador' \\
  -H 'accept: application/json' \\
  -H 'authorization: Bearer secret' \\
  -H 'content-type: application/json' \\
  -b 'session=secret' \\
  --data-raw '{"text":"original"}'
"""


class ApiClientTests(unittest.TestCase):
    def make_client(self) -> CurlKarasenaClient:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "request.curl"
        path.write_text(SAMPLE_CURL, encoding="utf-8")
        return CurlKarasenaClient(path)

    def test_parse_curl(self) -> None:
        client = self.make_client()
        self.assertEqual("https://example.test/api/comprobador", client.url)
        self.assertEqual("POST", client.method)
        self.assertEqual("original", client.body_template["text"])
        self.assertIn("authorization", {key.lower() for key in client.headers})
        self.assertEqual("session=secret", client.headers["Cookie"])

    def test_parse_allowed_response(self) -> None:
        self.assertEqual(("allow", ""), CurlKarasenaClient._parse_response(
            {"valido": True, "razon": None}
        ))

    def test_parse_blocked_response(self) -> None:
        self.assertEqual(("block", "Dato personal"), CurlKarasenaClient._parse_response(
            {"valido": False, "razon": "Dato personal"}
        ))

    def test_rejects_unknown_response(self) -> None:
        with self.assertRaises(RequestTemplateError):
            CurlKarasenaClient._parse_response({"allowed": True})

    def test_expired_jwt_is_rejected(self) -> None:
        client = self.make_client()
        client.token_expires_at = time.time() - 1
        with self.assertRaises(RequestTemplateError):
            client._validate_token_lifetime()


if __name__ == "__main__":
    unittest.main()
