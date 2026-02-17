"""Shared utilities for intermem modules."""
from __future__ import annotations


def normalize_content(content: str) -> str:
    """Normalize multiline entry content for matching."""
    return "\n".join(line.strip() for line in content.splitlines()).strip()
