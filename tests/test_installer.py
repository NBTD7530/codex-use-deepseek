import json
import plistlib
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path

from src.installer import (
    InstallError,
    InstallLayout,
    activate_launch_agent,
    extract_deepseek_key,
    install,
    install_keychain_secret,
    merge_catalog,
    migrate,
    port_is_available,
    render_launch_agent,
    rollback,
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


class LaunchAgentTests(unittest.TestCase):
    def test_launch_agent_is_loopback_keepalive_and_run_at_load(self):
        plist_bytes = render_launch_agent(
            Path("/Users/example"), Path("/usr/bin/python3")
        )
        payload = plistlib.loads(plist_bytes)

        self.assertEqual(payload["Label"], "com.codex.model-router")
        self.assertIs(payload["RunAtLoad"], True)
        self.assertIs(payload["KeepAlive"], True)
        self.assertEqual(payload["ProgramArguments"][0], "/usr/bin/python3")
        self.assertIn("127.0.0.1", payload["ProgramArguments"])
        self.assertIn("17890", payload["ProgramArguments"])
        self.assertIn(
            "/Users/example/.codex/models-router.json",
            payload["ProgramArguments"],
        )

    def test_activation_boots_out_an_old_job_then_bootstraps_and_kickstarts(self):
        calls = []

        def successful_runner(arguments, **kwargs):
            calls.append(arguments)
            return subprocess.CompletedProcess(arguments, 0, "", "")

        activate_launch_agent(
            Path("/Users/example/Library/LaunchAgents/com.codex.model-router.plist"),
            runner=successful_runner,
            uid=501,
        )

        self.assertEqual(
            calls,
            [
                [
                    "/bin/launchctl",
                    "bootout",
                    "gui/501/com.codex.model-router",
                ],
                [
                    "/bin/launchctl",
                    "bootstrap",
                    "gui/501",
                    "/Users/example/Library/LaunchAgents/com.codex.model-router.plist",
                ],
                [
                    "/bin/launchctl",
                    "kickstart",
                    "-k",
                    "gui/501/com.codex.model-router",
                ],
            ],
        )

    def test_port_check_detects_an_active_listener(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)

        self.assertFalse(port_is_available(listener.getsockname()[1]))


class InstallTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name)
        self.home = root / "home"
        self.project = root / "project"
        (self.home / ".codex").mkdir(parents=True)
        (self.project / "config").mkdir(parents=True)
        (self.project / "src").mkdir(parents=True)
        self.config_path = self.home / ".codex" / "config.toml"
        self.config_path.write_text(SAMPLE_CONFIG, encoding="utf-8")
        (self.home / ".codex" / "models_cache.json").write_text(
            json.dumps(
                {
                    "models": [
                        {"slug": "gpt-5.6-sol", "display_name": "GPT-5.6-Sol"}
                    ]
                }
            ),
            encoding="utf-8",
        )
        (self.project / "config" / "deepseek-models.json").write_text(
            json.dumps(
                {
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
            ),
            encoding="utf-8",
        )
        (self.project / "src" / "codex_model_router.py").write_text(
            "#!/usr/bin/env python3\n", encoding="utf-8"
        )
        self.layout = InstallLayout(home=self.home, project_root=self.project)

    def test_keychain_secret_is_passed_on_stdin_not_in_process_arguments(self):
        calls = []

        def recording_password_writer(arguments, secret):
            calls.append((arguments, secret))
            return subprocess.CompletedProcess(arguments, 0, "", "")

        install_keychain_secret(
            "dummy-deepseek-secret",
            password_writer=recording_password_writer,
            key_reader=lambda account: "dummy-deepseek-secret",
            account="example",
        )

        arguments, secret = calls[0]
        self.assertNotIn("dummy-deepseek-secret", arguments)
        self.assertEqual(secret, "dummy-deepseek-secret")
        self.assertEqual(arguments[-1], "-w")

    def test_keychain_write_is_rejected_when_readback_does_not_match(self):
        def successful_password_writer(arguments, secret):
            return subprocess.CompletedProcess(arguments, 0, "", "")

        with self.assertRaisesRegex(InstallError, "verification"):
            install_keychain_secret(
                "dummy-deepseek-secret",
                password_writer=successful_password_writer,
                key_reader=lambda account: "",
                account="example",
            )

    def test_failed_keychain_write_leaves_config_untouched(self):
        def successful_runner(arguments, **kwargs):
            return subprocess.CompletedProcess(arguments, 0, "", "")

        def failing_password_writer(arguments, secret):
            return subprocess.CompletedProcess(arguments, 1, "", "denied")

        with self.assertRaisesRegex(InstallError, "Keychain"):
            migrate(
                self.layout,
                runner=successful_runner,
                password_writer=failing_password_writer,
            )

        self.assertEqual(self.config_path.read_text(encoding="utf-8"), SAMPLE_CONFIG)
        self.assertFalse((self.home / ".codex" / "models-router.json").exists())
        self.assertFalse(
            (self.home / "Library/LaunchAgents/com.codex.model-router.plist").exists()
        )

    def test_successful_migration_writes_router_files_after_backup(self):
        def successful_runner(arguments, **kwargs):
            if "find-generic-password" in arguments:
                return subprocess.CompletedProcess(
                    arguments, 0, "dummy-deepseek-secret\n", ""
                )
            return subprocess.CompletedProcess(arguments, 0, "", "")

        def successful_password_writer(arguments, secret):
            return subprocess.CompletedProcess(arguments, 0, "", "")

        backup = migrate(
            self.layout,
            runner=successful_runner,
            password_writer=successful_password_writer,
        )

        self.assertTrue((backup / "config.toml").exists())
        self.assertEqual(
            (backup / "config.toml").read_text(encoding="utf-8"), SAMPLE_CONFIG
        )
        migrated = self.config_path.read_text(encoding="utf-8")
        self.assertIn('model_provider = "local_router"', migrated)
        self.assertNotIn("dummy-deepseek-secret", migrated)
        catalog = json.loads(
            (self.home / ".codex" / "models-router.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            [model["slug"] for model in catalog["models"]],
            ["gpt-5.6-sol", "deepseek-v4-flash", "deepseek-v4-pro"],
        )
        self.assertTrue(
            (self.home / ".codex/model-router/codex_model_router.py").exists()
        )
        self.assertTrue(
            (self.home / "Library/LaunchAgents/com.codex.model-router.plist").exists()
        )

    def test_reinstall_uses_an_existing_keychain_item(self):
        self.config_path.write_text(
            rewrite_codex_config(SAMPLE_CONFIG), encoding="utf-8"
        )
        calls = []

        def keychain_reader(arguments, **kwargs):
            calls.append(arguments)
            if "find-generic-password" in arguments:
                return subprocess.CompletedProcess(
                    arguments, 0, "existing-secret\n", ""
                )
            return subprocess.CompletedProcess(arguments, 1, "", "unexpected write")

        migrate(
            self.layout,
            runner=keychain_reader,
            password_writer=lambda arguments, secret: subprocess.CompletedProcess(
                arguments, 1, "", "must not write"
            ),
        )

        self.assertEqual(len(calls), 1)
        self.assertIn("find-generic-password", calls[0])

    def test_rollback_restores_config_and_removes_new_activation_files(self):
        def successful_runner(arguments, **kwargs):
            if "find-generic-password" in arguments:
                return subprocess.CompletedProcess(
                    arguments, 0, "dummy-deepseek-secret\n", ""
                )
            return subprocess.CompletedProcess(arguments, 0, "", "")

        backup = migrate(
            self.layout,
            runner=successful_runner,
            password_writer=lambda arguments, secret: subprocess.CompletedProcess(
                arguments, 0, "", ""
            ),
        )

        rollback(self.layout, backup, runner=successful_runner, uid=501)

        self.assertEqual(self.config_path.read_text(encoding="utf-8"), SAMPLE_CONFIG)
        self.assertFalse((self.home / ".codex/models-router.json").exists())
        self.assertFalse(
            (self.home / "Library/LaunchAgents/com.codex.model-router.plist").exists()
        )

    def test_launchd_failure_rolls_back_the_completed_file_migration(self):
        def runner(arguments, **kwargs):
            if "find-generic-password" in arguments:
                return subprocess.CompletedProcess(
                    arguments, 0, "dummy-deepseek-secret\n", ""
                )
            if "bootstrap" in arguments:
                return subprocess.CompletedProcess(arguments, 1, "", "denied")
            return subprocess.CompletedProcess(arguments, 0, "", "")

        with self.assertRaisesRegex(InstallError, "bootstrap"):
            install(
                self.layout,
                runner=runner,
                password_writer=lambda arguments, secret: subprocess.CompletedProcess(
                    arguments, 0, "", ""
                ),
                port_checker=lambda port: True,
            )

        self.assertEqual(self.config_path.read_text(encoding="utf-8"), SAMPLE_CONFIG)
        self.assertFalse((self.home / ".codex/models-router.json").exists())
        self.assertFalse(
            (self.home / "Library/LaunchAgents/com.codex.model-router.plist").exists()
        )


if __name__ == "__main__":
    unittest.main()
