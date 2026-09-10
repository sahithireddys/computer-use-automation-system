"""
Minimal Anthropic Messages API client.

Deliberately dependency-light: uses `requests` directly rather than the
`anthropic` SDK. This keeps the project's dependency surface to
{flask, playwright, pydantic, requests} and makes the raw request/response
shape easy to inspect/redact for evidence logging.
"""
from __future__ import annotations

import os
from typing import Any, Optional

import requests

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

# NOTE: model catalogs change; if this default 404s, check
# https://docs.claude.com for the current model id and override via
# the ANTHROPIC_MODEL env var. Updated 2026-09-10 to the current Claude 5
# generation (was "claude-sonnet-4-5").
DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")


class AnthropicError(Exception):
    pass


def call_claude(
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    max_tokens: int = 1024,
) -> dict[str, Any]:
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise AnthropicError(
            "ANTHROPIC_API_KEY is not set. Export it before running the discovery agent."
        )
    resp = requests.post(
        ANTHROPIC_API_URL,
        headers={
            "x-api-key": key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
        json={
            "model": model or DEFAULT_MODEL,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "tools": tools,
        },
        timeout=60,
    )
    if resp.status_code != 200:
        raise AnthropicError(f"Anthropic API error {resp.status_code}: {resp.text[:500]}")
    return resp.json()
