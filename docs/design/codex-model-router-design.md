# Codex Model Router Design

**Status:** Approved for implementation on 2026-07-31

## Goal

Keep a single model picker in the stock Codex desktop app while automatically using ChatGPT subscription authentication for GPT models and a DeepSeek API key for DeepSeek models. CC Switch must not participate in routing or credential lookup.

## User experience

- The existing Codex model picker contains the current GPT catalog plus `deepseek-v4-flash` and `deepseek-v4-pro`.
- Selecting a GPT model sends the request through the ChatGPT subscription endpoint with the Codex-managed login token.
- Selecting a DeepSeek model sends the request to DeepSeek's native Responses endpoint with the DeepSeek API key.
- The user launches the existing Codex app normally. There is no second icon, profile, or preparatory terminal command.
- A per-user macOS LaunchAgent starts the router at login and restarts it after an unexpected exit.

## Architecture

Codex uses a single custom provider whose base URL is `http://127.0.0.1:17890`. The provider keeps `requires_openai_auth = true`, causing Codex to attach its official ChatGPT login token. The local router reads the `model` field in each Responses request and selects exactly one upstream:

- GPT catalog entry: `https://chatgpt.com/backend-api/codex`
- `deepseek-v4-flash` or `deepseek-v4-pro`: `https://api.deepseek.com`
- Any uncatalogued model: local HTTP 400 response

The router forwards Responses streaming data without interpreting or rewriting events.

## Model catalog

The installer creates `~/.codex/models-router.json` by merging the current official GPT entries from `~/.codex/models_cache.json` with the two DeepSeek entries from the project. Hidden official models remain hidden. Duplicate model slugs are replaced deterministically by the project entry only for the two DeepSeek slugs.

`deepseek-v4-pro` remains visible even if DeepSeek has not enabled it for the account. An upstream availability error is returned unchanged; it must never fall back to Flash.

## Routing and header rules

The request body must be valid JSON and include a string `model`. The model must be present in the installed catalog.

For GPT requests, the router preserves `Authorization`, ChatGPT account identification, content negotiation, and Codex protocol headers. It replaces the destination host and removes hop-by-hop headers.

For DeepSeek requests, the router removes `Authorization`, `ChatGPT-Account-ID`, `OpenAI-Organization`, and `OpenAI-Project`, then adds `Authorization: Bearer <DeepSeek key>`. OpenAI credentials must never leave the machine on this branch.

The router binds only to `127.0.0.1`. Requests without an Authorization bearer header are rejected before routing.

## Credential storage

- ChatGPT credentials stay in Codex's official authentication storage. The router never persists or logs them.
- The DeepSeek API key is migrated from the current DeepSeek provider entry into the macOS login Keychain under service `codex-model-router.deepseek`.
- The plaintext DeepSeek provider entry is removed from `~/.codex/config.toml` only after Keychain storage succeeds.
- The router reads the DeepSeek key lazily. A missing DeepSeek key disables only DeepSeek requests; GPT routing continues to work.

## Installation and lifecycle

Installed files live under `~/.codex/model-router` with directory mode `0700` and file mode `0600` except executable scripts, which use `0700`.

The LaunchAgent label is `com.codex.model-router` and listens on fixed port `17890`. Installation checks that the port is available before activating the agent. It never selects a random replacement port.

The LaunchAgent uses `RunAtLoad = true` and `KeepAlive = true`. A health endpoint at `GET /health` returns the service state without exposing credentials.

## Configuration migration

Before making changes, the installer writes timestamped backups of:

- `~/.codex/config.toml`
- `~/.codex/models.json`, when present
- the installed LaunchAgent, when present

The migrated Codex config:

- defaults to `gpt-5.6-sol`
- sets `model_provider = "local_router"`
- removes `preferred_auth_method = "apikey"` and `forced_login_method = "api"`
- sets `model_catalog_json = "~/.codex/models-router.json"`
- adds `[model_providers.local_router]` with the loopback base URL, Responses wire protocol, and official authentication requirement
- removes `[model_providers.deepseek]` after successful Keychain migration
- preserves unrelated desktop, hooks, plugins, MCP, project, feature, and memory settings byte-for-byte wherever possible

## Logging and failures

Logs contain timestamp, model, selected upstream, status, latency, and an error category. They never contain request bodies, response bodies, cookies, authorization headers, account identifiers, or API keys.

The router returns explicit local errors for invalid JSON, missing model, unknown model, missing Codex bearer authentication, missing DeepSeek key, and upstream connection failure. It performs no retry and no cross-provider fallback.

## Acceptance criteria

1. Unit tests prove exact model routing and unknown-model rejection.
2. Unit tests prove credential separation in both directions.
3. An integration test proves streamed bytes reach the client before the upstream closes.
4. Installer tests prove unrelated TOML sections survive migration and plaintext DeepSeek credentials are removed only after secure storage succeeds.
5. `GET /health` succeeds after the LaunchAgent is loaded.
6. A real GPT request returns a sentinel response while `model = gpt-5.6-sol`.
7. A real DeepSeek Flash request returns a different sentinel response while `model = deepseek-v4-flash`.
8. `app-server model/list` shows GPT, DeepSeek Flash, and DeepSeek Pro in one list.
9. CC Switch proxy state and database are not used or modified.

## Rollback

The installer records a timestamped migration directory. Rollback unloads the LaunchAgent, restores the exact previous Codex config and catalog files, and leaves the Keychain item in place unless the user explicitly requests credential deletion.
