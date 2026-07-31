#!/usr/bin/env python3
"""Transactional installer helpers for the Codex model router."""

import json
import re


TOP_LEVEL_REPLACED_KEYS = frozenset(
    {
        "model",
        "model_provider",
        "model_catalog_json",
        "preferred_auth_method",
        "forced_login_method",
    }
)
REMOVED_PROVIDER_SECTIONS = frozenset(
    {"[model_providers.deepseek]", "[model_providers.local_router]"}
)
ROUTER_PROVIDER_BLOCK = '''[model_providers.local_router]
name = "OpenAI + DeepSeek"
base_url = "http://127.0.0.1:17890"
requires_openai_auth = true
supports_websockets = false
wire_api = "responses"
'''


def merge_catalog(cache, deepseek):
    """Merge current official entries with authoritative DeepSeek entries."""
    cache_models = cache.get("models")
    deepseek_models = deepseek.get("models")
    if not isinstance(cache_models, list) or not isinstance(deepseek_models, list):
        raise ValueError("both catalogs must contain a models list")

    deepseek_slugs = set()
    for model in deepseek_models:
        if not isinstance(model, dict) or not isinstance(model.get("slug"), str):
            raise ValueError("every DeepSeek model must contain a string slug")
        deepseek_slugs.add(model["slug"])

    official_models = []
    for model in cache_models:
        if not isinstance(model, dict) or not isinstance(model.get("slug"), str):
            raise ValueError("every official model must contain a string slug")
        if model["slug"] not in deepseek_slugs:
            official_models.append(model)

    return {"models": official_models + deepseek_models}


def extract_deepseek_key(text):
    """Return the plaintext key only from the legacy DeepSeek provider block."""
    current_section = None
    assignment = re.compile(
        r"^\s*experimental_bearer_token\s*=\s*([\"'])(.*?)\1\s*$"
    )
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current_section = stripped
            continue
        if current_section != "[model_providers.deepseek]":
            continue
        match = assignment.match(line)
        if match:
            quote, value = match.groups()
            return json.loads('"{0}"'.format(value)) if quote == '"' else value
    return None


def rewrite_codex_config(text):
    """Point Codex at the router while preserving unrelated TOML sections."""
    lines = text.splitlines(keepends=True)
    first_section = next(
        (
            index
            for index, line in enumerate(lines)
            if line.strip().startswith("[") and line.strip().endswith("]")
        ),
        len(lines),
    )

    top_level = []
    assignment = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=")
    for line in lines[:first_section]:
        match = assignment.match(line)
        if match and match.group(1) in TOP_LEVEL_REPLACED_KEYS:
            continue
        top_level.append(line)

    migrated_top = [
        'model = "gpt-5.6-sol"\n',
        'model_provider = "local_router"\n',
        'model_catalog_json = "~/.codex/models-router.json"\n',
    ]
    migrated_top.extend(top_level)

    retained_sections = []
    skipping = False
    for line in lines[first_section:]:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            skipping = stripped in REMOVED_PROVIDER_SECTIONS
        if not skipping:
            retained_sections.append(line)

    migrated = "".join(migrated_top + retained_sections).rstrip("\n")
    return migrated + "\n\n" + ROUTER_PROVIDER_BLOCK
