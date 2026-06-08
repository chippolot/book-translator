"""Curated model lists per provider.

These are the canonical models the GUI offers via dropdown. Users can
still type a custom model name (the dropdowns are editable). We sync
defaults with src/config.py.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in _sys.path:
    _sys.path.insert(0, str(_HERE.parent))

from config import (  # noqa: E402
    DEFAULT_TRANSCRIBE_MODELS, DEFAULT_TRANSLATE_MODELS,
)

PROVIDERS = ("google", "anthropic", "openai")
PROVIDER_LABELS = {
    "google": "Google Gemini",
    "anthropic": "Anthropic (Claude)",
    "openai": "OpenAI",
}

# Curated lists, newest first. Editable: the user can type a custom model.
TRANSCRIBE_MODELS = {
    "google": ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash"],
    "anthropic": ["claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
    "openai": ["gpt-5", "gpt-4o", "gpt-4o-mini"],
}

TRANSLATE_MODELS = {
    "google": ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash"],
    "anthropic": ["claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
    "openai": ["gpt-5", "gpt-4o", "gpt-4o-mini"],
}


def default_model(stage: str, provider: str) -> str:
    if stage == "transcribe":
        return DEFAULT_TRANSCRIBE_MODELS.get(provider, "")
    if stage == "translate":
        return DEFAULT_TRANSLATE_MODELS.get(provider, "")
    return ""
