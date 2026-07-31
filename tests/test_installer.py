import unittest

from src.installer import (
    extract_deepseek_key,
    merge_catalog,
    rewrite_codex_config,
)


SAMPLE_CONFIG = '''model = "deepseek-v4-flash"
model_provider = "deepseek"
model_reasoning_effort = "high"
preferred_auth_method = "apikey"
forced_login_method = "api"
model_catalog_json = "~/.codex/models.json"
notify = [
    "/Applications/Notifier",
    "turn-ended",
]

[desktop]
appearanceTheme = "light"

[model_providers.custom]
name = "OpenAI"
requires_openai_auth = true
wire_api = "responses"

[model_providers.deepseek]
name = "deepseek"
base_url = "https://api.deepseek.com/"
wire_api = "responses"
experimental_bearer_token = "dummy-deepseek-secret"

[plugins."browser@openai-bundled"]
enabled = true
'''


class CatalogTests(unittest.TestCase):
    def test_merge_preserves_official_models_and_replaces_deepseek_duplicates(self):
        cache = {
            "fetched_at": "ignored",
            "models": [
                {"slug": "gpt-5.6-sol", "display_name": "GPT-5.6-Sol"},
                {"slug": "deepseek-v4-flash", "display_name": "stale"},
            ],
        }
        deepseek = {
            "models": [
                {
                    "slug": "deepseek-v4-flash",
                    "display_name": "DeepSeek-V4-Flash",
                },
                {
                    "slug": "deepseek-v4-pro",
                    "display_name": "DeepSeek-V4-Pro",
                },
            ]
        }

        merged = merge_catalog(cache, deepseek)

        self.assertEqual(
            [model["slug"] for model in merged["models"]],
            ["gpt-5.6-sol", "deepseek-v4-flash", "deepseek-v4-pro"],
        )
        self.assertEqual(
            merged["models"][1]["display_name"], "DeepSeek-V4-Flash"
        )
        self.assertEqual(set(merged), {"models"})

    def test_merge_rejects_catalogs_without_model_lists(self):
        with self.assertRaisesRegex(ValueError, "models list"):
            merge_catalog({}, {"models": []})


class ConfigMigrationTests(unittest.TestCase):
    def test_extract_deepseek_key_only_reads_the_deepseek_provider(self):
        text = (
            'experimental_bearer_token = "wrong-top-level"\n'
            + SAMPLE_CONFIG
            + '\n[model_providers.other]\nexperimental_bearer_token = "wrong-other"\n'
        )

        self.assertEqual(extract_deepseek_key(text), "dummy-deepseek-secret")

    def test_rewrite_preserves_unrelated_sections_and_removes_plaintext_secret(self):
        rewritten = rewrite_codex_config(SAMPLE_CONFIG)

        self.assertIn('model = "gpt-5.6-sol"', rewritten)
        self.assertIn('model_provider = "local_router"', rewritten)
        self.assertIn(
            'model_catalog_json = "~/.codex/models-router.json"', rewritten
        )
        self.assertIn('model_reasoning_effort = "high"', rewritten)
        self.assertIn(
            'notify = [\n    "/Applications/Notifier",\n    "turn-ended",\n]',
            rewritten,
        )
        self.assertIn('[desktop]\nappearanceTheme = "light"', rewritten)
        self.assertIn('[model_providers.custom]', rewritten)
        self.assertIn('[plugins."browser@openai-bundled"]', rewritten)
        self.assertNotIn("dummy-deepseek-secret", rewritten)
        self.assertNotIn("[model_providers.deepseek]", rewritten)
        self.assertNotIn("preferred_auth_method", rewritten)
        self.assertNotIn("forced_login_method", rewritten)
        self.assertIn("[model_providers.local_router]", rewritten)
        self.assertIn('base_url = "http://127.0.0.1:17890"', rewritten)
        self.assertIn("requires_openai_auth = true", rewritten)

    def test_rewrite_is_idempotent(self):
        once = rewrite_codex_config(SAMPLE_CONFIG)

        self.assertEqual(rewrite_codex_config(once), once)


if __name__ == "__main__":
    unittest.main()
