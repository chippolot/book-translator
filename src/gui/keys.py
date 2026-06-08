"""Secure API-key storage backed by the OS keychain (via `keyring`).

Three keys: `anthropic`, `google`, `openai`. We avoid writing keys to disk.
A user with a legacy `.env` is supported as a read-only fallback so they
don't need to migrate before the first run; the GUI prompts to import
once on startup.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import keyring

SERVICE = "book-translate"

# Provider key id (lowercased) -> env var name used by the existing SDKs.
ENV_VARS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GEMINI_API_KEY",
    "openai": "OPENAI_API_KEY",
}

# Human-readable labels for the settings UI.
LABELS = {
    "anthropic": "Anthropic (Claude)",
    "google": "Google Gemini",
    "openai": "OpenAI",
}

PROVIDERS = tuple(ENV_VARS.keys())


def get(provider: str) -> Optional[str]:
    """Return the stored key for `provider`, or None if missing."""
    if provider not in ENV_VARS:
        return None
    try:
        v = keyring.get_password(SERVICE, provider)
    except Exception:  # noqa: BLE001 - keyring backends may throw
        v = None
    if v:
        return v
    # Fallback: read process env (which may have been populated by .env).
    return os.environ.get(ENV_VARS[provider]) or None


def set(provider: str, value: str) -> None:
    if provider not in ENV_VARS:
        raise ValueError(f"unknown provider: {provider}")
    keyring.set_password(SERVICE, provider, value)


def delete(provider: str) -> None:
    if provider not in ENV_VARS:
        return
    try:
        keyring.delete_password(SERVICE, provider)
    except Exception:  # noqa: BLE001 - not-found errors vary by backend
        pass


def import_from_env_file(env_path: Path) -> list[str]:
    """Read `env_path` and copy any of our three keys into the keychain.

    Returns the list of provider ids successfully imported.
    """
    if not env_path.exists():
        return []
    imported: list[str] = []
    text = env_path.read_text(encoding="utf-8", errors="ignore")
    env_to_provider = {v: k for k, v in ENV_VARS.items()}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if not v:
            continue
        provider = env_to_provider.get(k)
        if provider and not get(provider):
            try:
                set(provider, v)
                imported.append(provider)
            except Exception:  # noqa: BLE001
                pass
    return imported


def apply_to_env() -> dict[str, str]:
    """Set os.environ for whichever provider keys are stored.

    Returns the previous env values (so callers can restore them after a
    run). The pipeline picks keys up from os.environ.
    """
    prior: dict[str, str] = {}
    for provider, env in ENV_VARS.items():
        value = get(provider)
        prior[env] = os.environ.get(env, "")
        if value:
            os.environ[env] = value
    return prior


def status() -> dict[str, bool]:
    """{provider: has_key} for all three providers."""
    return {p: bool(get(p)) for p in PROVIDERS}
