# Codex Model Router Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and install a loopback Responses router that lets one Codex desktop model picker use ChatGPT subscription authentication for GPT and a Keychain-backed API key for DeepSeek.

**Architecture:** Codex sends every Responses request to one authenticated loopback provider. A Python standard-library service validates the requested model against the installed merged catalog, preserves the Codex bearer token only for the ChatGPT upstream, injects the Keychain secret only for the DeepSeek upstream, and streams the upstream response unchanged. A tested installer backs up and migrates Codex configuration and installs a macOS LaunchAgent.

**Tech Stack:** Python 3.9 standard library, `unittest`, macOS Keychain `security`, macOS `launchd`, TOML line-oriented migration, JSON model catalogs.

## Global Constraints

- Bind only to `127.0.0.1:17890`.
- Do not modify `ChatGPT.app` or use CC Switch files at runtime.
- Never log or persist request bodies, response bodies, ChatGPT tokens, account identifiers, or the DeepSeek API key.
- Reject unknown models and never fall back across providers.
- Preserve unrelated Codex configuration.
- Write a failing test and observe the expected failure before each production behavior.

---

### Task 1: Routing policy and credential isolation

**Files:**
- Create: `src/codex_model_router.py`
- Create: `tests/test_router.py`

**Interfaces:**
- Produces: `RoutingPolicy.from_catalog(path: Path) -> RoutingPolicy`
- Produces: `RoutingPolicy.choose(model: str) -> str`, returning `"openai"` or `"deepseek"`
- Produces: `prepare_upstream_headers(headers: Mapping[str, str], route: str, deepseek_key: Optional[str]) -> Dict[str, str]`

- [ ] **Step 1: Write failing policy tests**

```python
def test_catalog_models_route_to_the_expected_upstream(self):
    policy = RoutingPolicy.from_catalog(self.catalog_path)
    self.assertEqual(policy.choose("gpt-5.6-sol"), "openai")
    self.assertEqual(policy.choose("deepseek-v4-flash"), "deepseek")

def test_unknown_model_is_rejected(self):
    policy = RoutingPolicy.from_catalog(self.catalog_path)
    with self.assertRaises(UnknownModelError):
        policy.choose("unlisted-model")
```

- [ ] **Step 2: Run the policy tests and verify RED**

Run: `/usr/bin/python3 -m unittest tests.test_router.RoutingPolicyTests -v`

Expected: import failure because `src.codex_model_router` does not exist.

- [ ] **Step 3: Implement the minimal catalog policy**

```python
class UnknownModelError(ValueError):
    pass

class RoutingPolicy:
    def __init__(self, openai_models, deepseek_models):
        self.openai_models = frozenset(openai_models)
        self.deepseek_models = frozenset(deepseek_models)

    @classmethod
    def from_catalog(cls, path):
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        slugs = {item["slug"] for item in payload["models"]}
        deepseek = {slug for slug in slugs if slug in DEEPSEEK_MODELS}
        return cls(slugs - deepseek, deepseek)

    def choose(self, model):
        if model in self.deepseek_models:
            return "deepseek"
        if model in self.openai_models:
            return "openai"
        raise UnknownModelError(model)
```

- [ ] **Step 4: Run the policy tests and verify GREEN**

Run: `/usr/bin/python3 -m unittest tests.test_router.RoutingPolicyTests -v`

Expected: both tests pass.

- [ ] **Step 5: Write failing header-isolation tests**

```python
def test_openai_preserves_codex_auth_and_never_adds_deepseek_key(self):
    result = prepare_upstream_headers(self.codex_headers, "openai", "ds-secret")
    self.assertEqual(result["Authorization"], "Bearer chatgpt-token")
    self.assertNotIn("ds-secret", repr(result))

def test_deepseek_removes_openai_identity_and_injects_its_key(self):
    result = prepare_upstream_headers(self.codex_headers, "deepseek", "ds-secret")
    self.assertEqual(result["Authorization"], "Bearer ds-secret")
    self.assertNotIn("ChatGPT-Account-ID", result)
    self.assertNotIn("OpenAI-Organization", result)
```

- [ ] **Step 6: Run header tests and verify RED**

Run: `/usr/bin/python3 -m unittest tests.test_router.HeaderIsolationTests -v`

Expected: import failure for `prepare_upstream_headers`.

- [ ] **Step 7: Implement header filtering**

```python
def prepare_upstream_headers(headers, route, deepseek_key):
    result = {key: value for key, value in headers.items()
              if key.lower() not in HOP_BY_HOP_HEADERS | {"host", "content-length"}}
    if route == "deepseek":
        for key in list(result):
            if key.lower() in DEEPSEEK_STRIPPED_IDENTITY_HEADERS:
                del result[key]
        if not deepseek_key:
            raise MissingDeepSeekKeyError()
        result["Authorization"] = "Bearer " + deepseek_key
    return result
```

- [ ] **Step 8: Run all Task 1 tests and commit**

Run: `/usr/bin/python3 -m unittest tests.test_router -v`

Expected: all Task 1 tests pass without warnings.

Commit: `git add src tests && git commit -m "feat: add provider routing policy"`

### Task 2: Responses proxy and streaming

**Files:**
- Modify: `src/codex_model_router.py`
- Modify: `tests/test_router.py`

**Interfaces:**
- Consumes: `RoutingPolicy.choose` and `prepare_upstream_headers`
- Produces: `RouterServer((host, port), policy, upstreams, key_provider)`
- Produces: `GET /health` and `POST /responses`

- [ ] **Step 1: Write a failing integration test using a real local upstream server**

```python
def test_streaming_response_is_forwarded_chunk_by_chunk(self):
    with running_upstream([b"data: first\n\n", b"data: second\n\n"]) as upstream:
        with running_router(self.policy, upstream) as router:
            response = post_response(router, "gpt-5.6-sol", self.auth)
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"data: first\n\ndata: second\n\n")
```

- [ ] **Step 2: Run the streaming test and verify RED**

Run: `/usr/bin/python3 -m unittest tests.test_router.RouterIntegrationTests.test_streaming_response_is_forwarded_chunk_by_chunk -v`

Expected: import failure because `RouterServer` is missing.

- [ ] **Step 3: Implement request validation, upstream forwarding, and chunked response streaming**

```python
class RouterHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        body = self.rfile.read(require_content_length(self.headers))
        model = require_model(body)
        require_bearer(self.headers)
        route = self.server.policy.choose(model)
        key = self.server.key_provider() if route == "deepseek" else None
        headers = prepare_upstream_headers(self.headers, route, key)
        upstream = self.server.upstreams[route]
        response = upstream.request(self.path, body, headers)
        self.send_response(response.status)
        copy_end_to_end_headers(self, response.headers)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for chunk in response.iter_chunks():
            self.wfile.write(("%X\r\n" % len(chunk)).encode("ascii"))
            self.wfile.write(chunk + b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
```

- [ ] **Step 4: Run the streaming test and verify GREEN**

Run: `/usr/bin/python3 -m unittest tests.test_router.RouterIntegrationTests.test_streaming_response_is_forwarded_chunk_by_chunk -v`

Expected: the test passes.

- [ ] **Step 5: Add failing tests for health, missing bearer, invalid JSON, missing model, unknown model, missing DeepSeek key, and upstream failure**

```python
def test_missing_bearer_is_rejected_without_contacting_upstream(self):
    response = post_response(self.router, "gpt-5.6-sol", auth=None)
    self.assertEqual(response.status, 401)

def test_unknown_model_returns_400(self):
    response = post_response(self.router, "unknown", self.auth)
    self.assertEqual(response.status, 400)

def test_logs_never_contain_body_or_credentials(self):
    with self.assertLogs("codex_model_router", level="INFO") as captured:
        post_response(self.router, "gpt-5.6-sol", "Bearer private-token",
                      prompt="private-prompt")
    rendered = "\n".join(captured.output)
    self.assertNotIn("private-token", rendered)
    self.assertNotIn("private-prompt", rendered)
```

- [ ] **Step 6: Run error tests and verify RED, then implement exact JSON error responses**

Run: `/usr/bin/python3 -m unittest tests.test_router.RouterErrorTests -v`

Expected before implementation: failures for incorrect status codes. Expected after minimal implementation: all error tests pass with JSON bodies containing stable error codes.

- [ ] **Step 7: Run the full router suite and commit**

Run: `/usr/bin/python3 -m unittest tests.test_router -v`

Expected: all router tests pass.

Commit: `git add src tests && git commit -m "feat: proxy responses streams by model"`

### Task 3: Safe Codex configuration and catalog migration

**Files:**
- Create: `config/deepseek-models.json`
- Create: `src/installer.py`
- Create: `tests/test_installer.py`

**Interfaces:**
- Produces: `merge_catalog(cache: dict, deepseek: dict) -> dict`
- Produces: `rewrite_codex_config(text: str) -> str`
- Produces: `extract_deepseek_key(text: str) -> Optional[str]`

- [ ] **Step 1: Write failing catalog merge tests**

```python
def test_merge_preserves_gpt_and_replaces_deepseek_duplicates(self):
    merged = merge_catalog(self.cache, self.deepseek)
    self.assertEqual([m["slug"] for m in merged["models"]],
                     ["gpt-5.6-sol", "deepseek-v4-flash", "deepseek-v4-pro"])
```

- [ ] **Step 2: Run the merge test and verify RED**

Run: `/usr/bin/python3 -m unittest tests.test_installer.CatalogTests -v`

Expected: import failure because `src.installer` does not exist.

- [ ] **Step 3: Implement deterministic catalog merging and verify GREEN**

```python
def merge_catalog(cache, deepseek):
    deepseek_slugs = {item["slug"] for item in deepseek["models"]}
    official = [item for item in cache["models"] if item["slug"] not in deepseek_slugs]
    return {"models": official + deepseek["models"]}
```

Run: `/usr/bin/python3 -m unittest tests.test_installer.CatalogTests -v`

Expected: catalog tests pass.

- [ ] **Step 4: Write failing configuration migration tests with unrelated sections and a dummy secret**

```python
def test_rewrite_preserves_unrelated_sections_and_removes_plaintext_deepseek(self):
    rewritten = rewrite_codex_config(SAMPLE_CONFIG)
    self.assertIn('[desktop]\nappearanceTheme = "light"', rewritten)
    self.assertIn('model_provider = "local_router"', rewritten)
    self.assertNotIn("dummy-deepseek-secret", rewritten)
    self.assertNotIn("[model_providers.deepseek]", rewritten)
    self.assertIn("[model_providers.local_router]", rewritten)
```

- [ ] **Step 5: Run migration tests and verify RED, implement the line-oriented top-level and provider-block rewrite, then verify GREEN**

Run: `/usr/bin/python3 -m unittest tests.test_installer.ConfigMigrationTests -v`

Expected before implementation: missing function failure. Expected after implementation: all migration tests pass and the resulting text ends with a newline.

- [ ] **Step 6: Run all installer tests and commit**

Run: `/usr/bin/python3 -m unittest tests.test_installer -v`

Expected: all catalog and migration tests pass.

Commit: `git add config src/installer.py tests/test_installer.py && git commit -m "feat: migrate codex config and model catalog"`

### Task 4: Keychain, backups, and LaunchAgent installation

**Files:**
- Modify: `src/installer.py`
- Create: `scripts/install.sh`
- Create: `scripts/uninstall.sh`
- Modify: `tests/test_installer.py`

**Interfaces:**
- Produces: `install_keychain_secret(secret: str, runner: Callable) -> None`
- Produces: `render_launch_agent(home: Path, python: Path) -> str`
- Produces: timestamped `~/.codex/model-router/backups/<timestamp>/manifest.json`

- [ ] **Step 1: Write failing tests proving config is not rewritten when Keychain storage fails**

```python
def test_failed_keychain_write_leaves_config_untouched(self):
    with self.assertRaises(InstallError):
        migrate(self.layout, runner=failing_security_runner)
    self.assertEqual(self.config_path.read_text(), SAMPLE_CONFIG)
```

- [ ] **Step 2: Run the safety test and verify RED**

Run: `/usr/bin/python3 -m unittest tests.test_installer.InstallTransactionTests -v`

Expected: missing `migrate` failure.

- [ ] **Step 3: Implement backup-first transactional installation**

```python
def migrate(layout, runner):
    backup = create_backup(layout)
    secret = extract_deepseek_key(layout.config.read_text())
    if secret:
        install_keychain_secret(secret, runner)
    write_atomic(layout.catalog, build_catalog(layout))
    write_atomic(layout.config, rewrite_codex_config(layout.config.read_text()))
    write_atomic(layout.launch_agent, render_launch_agent(layout.home, Path("/usr/bin/python3")))
    return backup
```

- [ ] **Step 4: Verify the transaction test passes**

Run: `/usr/bin/python3 -m unittest tests.test_installer.InstallTransactionTests -v`

Expected: failure simulation leaves the original config byte-identical.

- [ ] **Step 5: Add and pass plist assertions**

```python
def test_launch_agent_is_loopback_keepalive_and_run_at_load(self):
    plist = plistlib.loads(render_launch_agent(self.home, self.python).encode())
    self.assertEqual(plist["RunAtLoad"], True)
    self.assertEqual(plist["KeepAlive"], True)
    self.assertIn("127.0.0.1", plist["ProgramArguments"])
    self.assertIn("17890", plist["ProgramArguments"])
```

Run: `/usr/bin/python3 -m unittest tests.test_installer.LaunchAgentTests -v`

Expected: all plist assertions pass.

- [ ] **Step 6: Add shell entry points that call the tested installer and provide rollback without deleting Keychain credentials**

```sh
#!/bin/sh
set -eu
exec /usr/bin/python3 "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)/src/installer.py" install
```

- [ ] **Step 7: Run all tests and commit**

Run: `/usr/bin/python3 -m unittest discover -s tests -v`

Expected: all tests pass without network access or real Keychain mutations.

Commit: `git add src scripts tests && git commit -m "feat: install keychain-backed launch agent"`

### Task 5: Local installation and end-to-end verification

**Files:**
- Create: `README.md`
- Modify outside repository: `~/.codex/config.toml`
- Create outside repository: `~/.codex/models-router.json`
- Create outside repository: `~/.codex/model-router/`
- Create outside repository: `~/Library/LaunchAgents/com.codex.model-router.plist`

**Interfaces:**
- Consumes all previous project interfaces.
- Produces a running loopback router and one mixed Codex model picker.

- [ ] **Step 1: Run the complete test suite before installation**

Run: `/usr/bin/python3 -m unittest discover -s tests -v`

Expected: every test passes.

- [ ] **Step 2: Verify port 17890 is free and create timestamped backups**

Run: `lsof -nP -iTCP:17890 -sTCP:LISTEN`

Expected: no output before installation.

- [ ] **Step 3: Run the tested installer**

Run: `./scripts/install.sh`

Expected: installer reports the backup directory, successful Keychain migration, installed files, and loaded LaunchAgent without printing the key.

- [ ] **Step 4: Verify service health and file permissions**

Run: `curl --fail --silent http://127.0.0.1:17890/health`

Expected: JSON with `{"status":"ok"}` and no credential fields.

Run: `stat -f '%Sp %N' ~/.codex/model-router ~/.codex/model-router/*`

Expected: directory and executable modes are owner-only.

- [ ] **Step 5: Restore official ChatGPT login if Codex reports no authenticated account**

Run: `/Applications/ChatGPT.app/Contents/Resources/codex login`

Expected: the official login flow completes and Codex manages its own authentication state. Do not import an OAuth token from CC Switch.

- [ ] **Step 6: Verify the mixed model list**

Run the app-server `model/list` request and inspect model IDs.

Expected: all current GPT list entries plus `deepseek-v4-flash` and `deepseek-v4-pro` appear in the same response.

- [ ] **Step 7: Verify real GPT and DeepSeek requests**

Run GPT with a sentinel prompt and expect exactly `GPT_ROUTER_OK`. Run DeepSeek Flash with a different sentinel and expect exactly `DEEPSEEK_ROUTER_OK`. Inspect sanitized router logs to confirm different upstream labels and absence of request content.

- [ ] **Step 8: Document operation, diagnostics, upgrade behavior, and rollback**

README must include health check, LaunchAgent status, sanitized log paths, rerunning the installer after a model catalog change, and rollback commands. It must explicitly state that CC Switch is not required.

- [ ] **Step 9: Final verification and commit**

Run: `/usr/bin/python3 -m unittest discover -s tests -v && git status --short`

Expected: tests pass and only the intended README or plan checkbox changes remain.

Commit: `git add README.md docs && git commit -m "docs: add router operations guide"`
