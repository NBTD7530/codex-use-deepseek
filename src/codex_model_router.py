#!/usr/bin/env python3
"""Local Responses router for mixed OpenAI and DeepSeek Codex models."""

import json
from pathlib import Path


DEEPSEEK_MODELS = frozenset({"deepseek-v4-flash", "deepseek-v4-pro"})
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
