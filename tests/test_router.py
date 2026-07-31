import json
import tempfile
import unittest
from pathlib import Path

from src.codex_model_router import (
    MissingDeepSeekKeyError,
    RoutingPolicy,
    UnknownModelError,
    prepare_upstream_headers,
)


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


if __name__ == "__main__":
    unittest.main()
