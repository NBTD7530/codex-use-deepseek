#!/usr/bin/env python3
"""Local Responses router for mixed OpenAI and DeepSeek Codex models."""

import argparse
import json
import http.client
import logging
import os
import pwd
import re
import subprocess
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional


DEEPSEEK_MODELS = frozenset({"deepseek-flash", "deepseek-v4-pro"})
LOGGER = logging.getLogger("codex_model_router")
LOGGER.addHandler(logging.NullHandler())
HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
DEEPSEEK_STRIPPED_IDENTITY_HEADERS = frozenset(
    {
        "authorization",
        "chatgpt-account-id",
        "openai-organization",
        "openai-project",
    }
)


class UnknownModelError(ValueError):
    """Raised when a request names a model outside the installed catalog."""


class MissingDeepSeekKeyError(RuntimeError):
    """Raised when DeepSeek routing is requested without a Keychain secret."""


def normalize_deepseek_payload(payload):
    """Preserve orphan Codex tool results as ordinary DeepSeek input context."""
    inputs = payload.get("input") if isinstance(payload, dict) else None
    if not isinstance(inputs, list):
        return payload

    normalized_inputs = []
    changed = False
    for item in inputs:
        is_orphan_output = (
            isinstance(item, dict)
            and item.get("type") == "function_call_output"
            and not isinstance(item.get("call_id"), str)
        )
        if not is_orphan_output:
            normalized_inputs.append(item)
            continue

        source_parts = [
            value
            for value in (item.get("namespace"), item.get("name"))
            if isinstance(value, str) and value
        ]
        source = ".".join(source_parts)
        prefix = "Tool output from {0}:".format(source) if source else "Tool output:"
        output = item.get("output")
        if not isinstance(output, str):
            output = json.dumps(output, ensure_ascii=False, separators=(",", ":"))
        normalized_inputs.append(
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "{0}\n{1}".format(prefix, output)}
                ],
            }
        )
        changed = True

    if not changed:
        return payload
    normalized_payload = dict(payload)
    normalized_payload["input"] = normalized_inputs
    return normalized_payload


class RoutingPolicy:
    """Maps cataloged model identifiers to an explicit upstream."""

    def __init__(self, openai_models, deepseek_models):
        self.openai_models = frozenset(openai_models)
        self.deepseek_models = frozenset(deepseek_models)

    @classmethod
    def from_catalog(cls, path):
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        models = payload.get("models")
        if not isinstance(models, list):
            raise ValueError("model catalog must contain a models list")

        slugs = set()
        for item in models:
            if not isinstance(item, dict) or not isinstance(item.get("slug"), str):
                raise ValueError("every model catalog entry must contain a string slug")
            slugs.add(item["slug"])

        deepseek_models = slugs.intersection(DEEPSEEK_MODELS)
        return cls(slugs.difference(deepseek_models), deepseek_models)

    def choose(self, model):
        if model in self.deepseek_models:
            return "deepseek"
        if model in self.openai_models:
            return "openai"
        raise UnknownModelError("unknown model: {0}".format(model))


def prepare_upstream_headers(headers, route, deepseek_key):
    """Remove transport headers and isolate provider credentials."""
    excluded = HOP_BY_HOP_HEADERS.union({"host", "content-length"})
    result = {
        key: value for key, value in headers.items() if key.lower() not in excluded
    }

    if route == "deepseek":
        result = {
            key: value
            for key, value in result.items()
            if key.lower() not in DEEPSEEK_STRIPPED_IDENTITY_HEADERS
        }
        if not deepseek_key:
            raise MissingDeepSeekKeyError("DeepSeek API key is unavailable")
        result["Authorization"] = "Bearer {0}".format(deepseek_key)
    elif route != "openai":
        raise ValueError("unsupported route: {0}".format(route))

    return result


@dataclass(frozen=True)
class UpstreamTarget:
    """Connection details for one Responses-compatible upstream."""

    scheme: str
    host: str
    port: int
    base_path: str
    proxy_host: Optional[str] = None
    proxy_port: Optional[int] = None

    def request(self, path, body, headers):
        if self.scheme == "https" and self.proxy_host and self.proxy_port:
            connection = http.client.HTTPSConnection(
                self.proxy_host, self.proxy_port, timeout=600
            )
            connection.set_tunnel(self.host, self.port)
        else:
            connection_class = (
                http.client.HTTPSConnection
                if self.scheme == "https"
                else http.client.HTTPConnection
            )
            connection = connection_class(self.host, self.port, timeout=600)
        upstream_path = "{0}/{1}".format(
            self.base_path.rstrip("/"), path.lstrip("/")
        )
        connection.request("POST", upstream_path, body=body, headers=headers)
        return UpstreamResponse(connection, connection.getresponse())


class UpstreamResponse:
    """Owns an upstream HTTP response until its stream is exhausted."""

    def __init__(self, connection, response):
        self._connection = connection
        self._response = response
        self.status = response.status
        self.reason = response.reason
        self.headers = response.getheaders()

    def iter_chunks(self):
        try:
            while True:
                chunk = self._response.read1(64 * 1024)
                if not chunk:
                    return
                yield chunk
        finally:
            self._connection.close()


class RouterHandler(BaseHTTPRequestHandler):
    """Validates and forwards one Responses API request."""

    protocol_version = "HTTP/1.1"
    server_version = "CodexModelRouter/1"
    sys_version = ""

    def do_GET(self):
        if self.path != "/health":
            self._send_json(404, {"error": {"code": "not_found"}})
            return
        self._send_json(200, {"status": "ok"})

    def do_POST(self):
        started_at = time.monotonic()
        model = None
        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._reject(400, "invalid_content_length", model, started_at)
            return
        if content_length < 1:
            self._reject(400, "missing_request_body", model, started_at)
            return

        body = self.rfile.read(content_length)
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._reject(400, "invalid_json", model, started_at)
            return

        model = payload.get("model") if isinstance(payload, dict) else None
        if not isinstance(model, str) or not model:
            self._reject(400, "missing_model", None, started_at)
            return

        authorization = self.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            self._reject(401, "missing_codex_auth", model, started_at)
            return

        try:
            route = self.server.policy.choose(model)
        except UnknownModelError:
            self._reject(400, "unknown_model", model, started_at)
            return

        if route == "deepseek":
            normalized_payload = normalize_deepseek_payload(payload)
            if normalized_payload is not payload:
                body = json.dumps(
                    normalized_payload, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")

        try:
            deepseek_key = (
                self.server.key_provider() if route == "deepseek" else None
            )
            headers = prepare_upstream_headers(self.headers, route, deepseek_key)
        except MissingDeepSeekKeyError:
            self._reject(503, "missing_deepseek_key", model, started_at)
            return

        try:
            upstream_response = self.server.upstreams[route].request(
                self.path, body, headers
            )
        except (OSError, http.client.HTTPException, TimeoutError):
            self._reject(502, "upstream_unavailable", model, started_at)
            return

        self.send_response(upstream_response.status, upstream_response.reason)
        for key, value in upstream_response.headers:
            if key.lower() not in HOP_BY_HOP_HEADERS.union(
                {"content-length", "server", "date"}
            ):
                self.send_header(key, value)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for chunk in upstream_response.iter_chunks():
            self.wfile.write("{0:X}\r\n".format(len(chunk)).encode("ascii"))
            self.wfile.write(chunk)
            self.wfile.write(b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()
        LOGGER.info(
            "request model=%s route=%s status=%s duration_ms=%d",
            model,
            route,
            upstream_response.status,
            round((time.monotonic() - started_at) * 1000),
        )

    def _reject(self, status, code, model, started_at):
        self._send_json(status, {"error": {"code": code}})
        LOGGER.warning(
            "request model=%s route=none status=%s error=%s duration_ms=%d",
            model or "none",
            status,
            code,
            round((time.monotonic() - started_at) * 1000),
        )

    def _send_json(self, status, payload):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def log_message(self, format_string, *args):
        return


class RouterServer(ThreadingHTTPServer):
    """Threaded loopback server with explicit routing dependencies."""

    daemon_threads = True

    def __init__(self, address, policy, upstreams, key_provider):
        super().__init__(address, RouterHandler)
        self.policy = policy
        self.upstreams = upstreams
        self.key_provider = key_provider


def read_deepseek_key(runner=subprocess.run, account=None):
    """Read the DeepSeek key from the current user's login Keychain."""
    account_name = account or pwd.getpwuid(os.getuid()).pw_name
    result = runner(
        [
            "/usr/bin/security",
            "find-generic-password",
            "-a",
            account_name,
            "-s",
            "codex-model-router.deepseek",
            "-w",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    secret = result.stdout.strip()
    return secret or None


def discover_macos_https_proxy(runner=subprocess.run):
    """Read the enabled HTTPS proxy from macOS SystemConfiguration."""
    result = runner(
        ["/usr/sbin/scutil", "--proxy"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    values = {}
    for line in result.stdout.splitlines():
        match = re.match(r"^\s*(HTTPSEnable|HTTPSProxy|HTTPSPort)\s*:\s*(.*?)\s*$", line)
        if match:
            values[match.group(1)] = match.group(2)
    if values.get("HTTPSEnable") != "1":
        return None
    proxy_host = values.get("HTTPSProxy")
    proxy_port = values.get("HTTPSPort")
    if not proxy_host or not proxy_port:
        return None
    try:
        return proxy_host, int(proxy_port)
    except ValueError:
        return None


def build_production_server(
    host,
    port,
    catalog,
    key_provider=read_deepseek_key,
    proxy_discovery=discover_macos_https_proxy,
):
    """Create the fixed-upstream production server."""
    if host != "127.0.0.1":
        raise ValueError("router must bind to the IPv4 loopback address")
    policy = RoutingPolicy.from_catalog(catalog)
    https_proxy = proxy_discovery()
    proxy_host, proxy_port = https_proxy if https_proxy else (None, None)
    upstreams = {
        "openai": UpstreamTarget(
            scheme="https",
            host="chatgpt.com",
            port=443,
            base_path="/backend-api/codex",
            proxy_host=proxy_host,
            proxy_port=proxy_port,
        ),
        "deepseek": UpstreamTarget(
            scheme="https",
            host="api.deepseek.com",
            port=443,
            base_path="",
        ),
    }
    return RouterServer((host, port), policy, upstreams, key_provider)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=17890)
    parser.add_argument(
        "--catalog",
        type=Path,
        default=Path.home() / ".codex" / "models-router.json",
    )
    arguments = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    server = build_production_server(
        arguments.host, arguments.port, arguments.catalog
    )
    LOGGER.info("router_started host=%s port=%s", arguments.host, arguments.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
