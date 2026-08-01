# Codex Model Router

This local router lets the stock Codex desktop model picker use two authentication paths without CC Switch:

- GPT models use the official ChatGPT login managed by Codex.
- `deepseek-v4-flash` and `deepseek-v4-pro` use a DeepSeek API key stored in the macOS login Keychain.

Codex sends Responses requests to `http://127.0.0.1:17890`. The router validates the selected model against the installed catalog, chooses one upstream, isolates credentials, and streams the upstream response back unchanged.

## Documentation

- [PROJECT.md](docs/PROJECT.md): 项目总览，含需求背景、关键决策、架构与验证结果
- [REUSE.md](docs/REUSE.md): 面向他人的复用与交付说明
- [DESIGN.md](docs/design/codex-model-router-design.md): 原始设计规格与验收标准

## Installed components

- Router: `~/.codex/model-router/codex_model_router.py`
- Mixed model catalog: `~/.codex/models-router.json`
- LaunchAgent: `~/Library/LaunchAgents/com.codex.model-router.plist`
- Keychain service: `codex-model-router.deepseek`
- Logs: `~/.codex/model-router/logs/`
- Timestamped backups: `~/.codex/model-router/backups/`

The LaunchAgent uses `RunAtLoad` and `KeepAlive`, so the service starts at macOS login and restarts after an unexpected exit. It binds only to IPv4 loopback.

## Verification

Check health:

```sh
curl --fail --silent http://127.0.0.1:17890/health
```

Expected response:

```json
{"status":"ok"}
```

Check LaunchAgent status:

```sh
launchctl print "gui/$(id -u)/com.codex.model-router"
```

Check the sanitized request log:

```sh
tail -50 ~/.codex/model-router/logs/router-error.log
```

Request logs contain model, route, status, and duration. They do not contain prompt text, response text, account identifiers, or credentials.

Check Codex login status:

```sh
/Applications/ChatGPT.app/Contents/Resources/codex login status
```

Expected output is `Logged in using ChatGPT`.

## Tests

The project uses only the Python 3.9 standard library:

```sh
PYTHONWARNINGS=error /usr/bin/python3 -m unittest discover -s tests -v
```

## Reinstall after a router update

Stop the currently installed copy, then run the installer from this project:

```sh
launchctl bootout "gui/$(id -u)/com.codex.model-router"
./scripts/install.sh
```

The installer checks that port `17890` is available, creates another timestamped backup, verifies the existing Keychain item, refreshes the mixed catalog, installs the new router, and restarts the LaunchAgent.

The router discovers the enabled macOS HTTPS proxy when it starts. Restart the LaunchAgent after changing system proxy settings.

## Rollback

Restore the most recent pre-install Codex configuration and LaunchAgent state:

```sh
./scripts/rollback.sh
```

Restore a specific backup:

```sh
./scripts/rollback.sh ~/.codex/model-router/backups/20260731-195249-974399
```

Rollback unloads the router and restores the exact backed-up configuration/catalog files. It deliberately leaves the Keychain item and installed source in place so credentials are not deleted implicitly.

## Uninstall

Restore the pre-install Codex configuration and remove the router activation files:

```sh
./scripts/uninstall.sh
```

Uninstall restores the latest backup, unloads and deletes the LaunchAgent, and removes the installed router and logs. The Keychain item and timestamped backups are preserved; delete the credential manually if you also want to remove it:

```sh
security delete-generic-password -a "$(id -un)" -s "codex-model-router.deepseek"
```

## Model availability

The installed picker includes current GPT entries, DeepSeek V4 Flash, and DeepSeek V4 Pro. The router never falls back between models. If DeepSeek has not enabled Pro for the account, Codex displays the upstream availability error and Flash remains separately selectable.

## Security notes

- OpenAI authentication is forwarded only to `chatgpt.com` inside TLS.
- DeepSeek routing strips OpenAI authentication and account headers before adding the DeepSeek key.
- The macOS HTTPS proxy is used through an HTTP CONNECT tunnel; bearer headers remain inside target TLS.
- Unknown models and requests without a Codex bearer header are rejected locally.
- CC Switch is not queried, modified, or required at runtime.
