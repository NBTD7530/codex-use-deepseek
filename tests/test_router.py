import http.client
import json
import tempfile
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from src.codex_model_router import (
    MissingDeepSeekKeyError,
    RouterServer,
    RoutingPolicy,
    UnknownModelError,
    UpstreamTarget,
    prepare_upstream_headers,
)


class ScriptedUpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        self.server.request_count += 1
        length = int(self.headers["Content-Length"])
        self.server.received_body = self.rfile.read(length)
        self.server.received_headers = dict(self.headers)
        self.server.received_path = self.path
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(self.server.chunks[0])
        self.wfile.flush()
        self.server.first_chunk_sent.set()
        self.server.release_remaining.wait(timeout=3)
        for chunk in self.server.chunks[1:]:
            self.wfile.write(chunk)
            self.wfile.flush()
        self.close_connection = True

    def log_message(self, format_string, *args):
        return


@contextmanager
def running_server(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def new_upstream(chunks):
    server = ThreadingHTTPServer(("127.0.0.1", 0), ScriptedUpstreamHandler)
    server.chunks = chunks
    server.first_chunk_sent = threading.Event()
    server.release_remaining = threading.Event()
    server.received_body = None
    server.received_headers = None
    server.received_path = None
    server.request_count = 0
    return server


def request_router(router, method="POST", body=None, authorization="Bearer token"):
    connection = http.client.HTTPConnection(
        "127.0.0.1", router.server_address[1], timeout=3
    )
    headers = {"Content-Type": "application/json"}
    if authorization is not None:
        headers["Authorization"] = authorization
    connection.request(method, "/health" if method == "GET" else "/responses", body, headers)
    response = connection.getresponse()
    payload = response.read()
    status = response.status
    connection.close()
    return status, payload


class RoutingPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.catalog_path = Path(self.temporary_directory.name) / "models.json"
        self.catalog_path.write_text(
            json.dumps(
                {
                    "models": [
                        {"slug": "gpt-5.6-sol"},
                        {"slug": "codex-auto-review"},
                        {"slug": "deepseek-v4-flash"},
                        {"slug": "deepseek-v4-pro"},
                    ]
                }
            ),
            encoding="utf-8",
        )

    def test_catalog_models_route_to_the_expected_upstream(self):
        policy = RoutingPolicy.from_catalog(self.catalog_path)

        self.assertEqual(policy.choose("gpt-5.6-sol"), "openai")
        self.assertEqual(policy.choose("codex-auto-review"), "openai")
        self.assertEqual(policy.choose("deepseek-v4-flash"), "deepseek")
        self.assertEqual(policy.choose("deepseek-v4-pro"), "deepseek")

    def test_unknown_model_is_rejected(self):
        policy = RoutingPolicy.from_catalog(self.catalog_path)

        with self.assertRaisesRegex(UnknownModelError, "unlisted-model"):
            policy.choose("unlisted-model")


class HeaderIsolationTests(unittest.TestCase):
    def setUp(self):
        self.codex_headers = {
            "Authorization": "Bearer chatgpt-token",
            "ChatGPT-Account-ID": "account-id",
            "OpenAI-Organization": "organization-id",
            "OpenAI-Project": "project-id",
            "Content-Type": "application/json",
            "Connection": "keep-alive",
            "Host": "127.0.0.1:17890",
            "Content-Length": "123",
        }

    def test_openai_preserves_codex_auth_and_never_adds_deepseek_key(self):
        result = prepare_upstream_headers(
            self.codex_headers, "openai", "ds-secret"
        )

        self.assertEqual(result["Authorization"], "Bearer chatgpt-token")
        self.assertEqual(result["ChatGPT-Account-ID"], "account-id")
        self.assertNotIn("ds-secret", repr(result))
        self.assertNotIn("Connection", result)
        self.assertNotIn("Host", result)
        self.assertNotIn("Content-Length", result)

    def test_deepseek_removes_openai_identity_and_injects_its_key(self):
        result = prepare_upstream_headers(
            self.codex_headers, "deepseek", "ds-secret"
        )

        self.assertEqual(result["Authorization"], "Bearer ds-secret")
        self.assertEqual(result["Content-Type"], "application/json")
        self.assertNotIn("ChatGPT-Account-ID", result)
        self.assertNotIn("OpenAI-Organization", result)
        self.assertNotIn("OpenAI-Project", result)
        self.assertNotIn("chatgpt-token", repr(result))

    def test_deepseek_rejects_a_missing_api_key(self):
        with self.assertRaises(MissingDeepSeekKeyError):
            prepare_upstream_headers(self.codex_headers, "deepseek", None)


class RouterIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.catalog_path = Path(self.temporary_directory.name) / "models.json"
        self.catalog_path.write_text(
            json.dumps(
                {
                    "models": [
                        {"slug": "gpt-5.6-sol"},
                        {"slug": "deepseek-v4-flash"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.policy = RoutingPolicy.from_catalog(self.catalog_path)

    def test_streaming_response_is_forwarded_before_upstream_closes(self):
        first = b"data: first\n\n"
        second = b"data: second\n\n"
        upstream = new_upstream([first, second])
        upstream_target = UpstreamTarget(
            scheme="http",
            host="127.0.0.1",
            port=upstream.server_address[1],
            base_path="/backend-api/codex",
        )
        router = RouterServer(
            ("127.0.0.1", 0),
            self.policy,
            {"openai": upstream_target, "deepseek": upstream_target},
            key_provider=lambda: "ds-secret",
        )

        with running_server(upstream), running_server(router):
            connection = http.client.HTTPConnection(
                "127.0.0.1", router.server_address[1], timeout=3
            )
            payload = json.dumps(
                {"model": "gpt-5.6-sol", "input": "private-prompt"}
            ).encode("utf-8")
            connection.request(
                "POST",
                "/responses",
                body=payload,
                headers={
                    "Authorization": "Bearer chatgpt-token",
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            self.assertTrue(upstream.first_chunk_sent.wait(timeout=1))

            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(len(first)), first)
            upstream.release_remaining.set()
            self.assertEqual(response.read(), second)
            connection.close()

        self.assertEqual(upstream.received_path, "/backend-api/codex/responses")
        self.assertEqual(
            upstream.received_headers["Authorization"], "Bearer chatgpt-token"
        )


class RouterErrorTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        catalog_path = Path(self.temporary_directory.name) / "models.json"
        catalog_path.write_text(
            json.dumps(
                {
                    "models": [
                        {"slug": "gpt-5.6-sol"},
                        {"slug": "deepseek-v4-flash"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.policy = RoutingPolicy.from_catalog(catalog_path)
        self.upstream = new_upstream([b'{"ok":true}'])
        self.addCleanup(self.upstream.server_close)
        self.upstream.release_remaining.set()
        self.target = UpstreamTarget(
            scheme="http",
            host="127.0.0.1",
            port=self.upstream.server_address[1],
            base_path="",
        )

    def new_router(self, key_provider=lambda: "ds-secret"):
        return RouterServer(
            ("127.0.0.1", 0),
            self.policy,
            {"openai": self.target, "deepseek": self.target},
            key_provider=key_provider,
        )

    def post(self, router, payload, authorization="Bearer token"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return request_router(router, body=body, authorization=authorization)

    def test_health_reports_ok_without_contacting_upstream(self):
        router = self.new_router()
        with running_server(self.upstream), running_server(router):
            status, body = request_router(router, method="GET")

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"status": "ok"})
        self.assertEqual(self.upstream.request_count, 0)

    def test_missing_bearer_is_rejected_without_contacting_upstream(self):
        router = self.new_router()
        with running_server(self.upstream), running_server(router):
            status, body = self.post(
                router, {"model": "gpt-5.6-sol"}, authorization=None
            )

        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"]["code"], "missing_codex_auth")
        self.assertEqual(self.upstream.request_count, 0)

    def test_invalid_json_returns_400(self):
        router = self.new_router()
        with running_server(self.upstream), running_server(router):
            status, body = self.post(router, b"not-json")

        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_json")

    def test_missing_model_returns_400(self):
        router = self.new_router()
        with running_server(self.upstream), running_server(router):
            status, body = self.post(router, {"input": "hello"})

        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "missing_model")

    def test_unknown_model_returns_400(self):
        router = self.new_router()
        with running_server(self.upstream), running_server(router):
            status, body = self.post(router, {"model": "unknown"})

        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "unknown_model")

    def test_missing_deepseek_key_returns_503(self):
        router = self.new_router(key_provider=lambda: None)
        with running_server(self.upstream), running_server(router):
            status, body = self.post(router, {"model": "deepseek-v4-flash"})

        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body)["error"]["code"], "missing_deepseek_key")

    def test_upstream_connection_failure_returns_502(self):
        unreachable = UpstreamTarget("http", "127.0.0.1", 1, "")
        router = RouterServer(
            ("127.0.0.1", 0),
            self.policy,
            {"openai": unreachable, "deepseek": unreachable},
            key_provider=lambda: "ds-secret",
        )
        with running_server(router):
            status, body = self.post(router, {"model": "gpt-5.6-sol"})

        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body)["error"]["code"], "upstream_unavailable")

    def test_logs_never_contain_body_or_credentials(self):
        router = self.new_router()
        with running_server(self.upstream), running_server(router):
            with self.assertLogs("codex_model_router", level="INFO") as captured:
                status, _ = self.post(
                    router,
                    {"model": "gpt-5.6-sol", "input": "private-prompt"},
                    authorization="Bearer private-token",
                )

        self.assertEqual(status, 200)
        rendered = "\n".join(captured.output)
        self.assertIn("model=gpt-5.6-sol", rendered)
        self.assertNotIn("private-token", rendered)
        self.assertNotIn("private-prompt", rendered)


if __name__ == "__main__":
    unittest.main()
