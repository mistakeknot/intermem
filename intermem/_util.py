"""Shared utilities for intermem modules."""
from __future__ import annotations

import hashlib

from intermem.scanner import MemoryEntry


def normalize_content(content: str) -> str:
    """Normalize multiline entry content for matching."""
    return "\n".join(line.strip() for line in content.splitlines()).strip()


def hash_entry(entry: MemoryEntry) -> str:
    """Canonical content hash for an entry (SHA-256 truncated to 16 hex chars)."""
    return hashlib.sha256(entry.content.strip().encode("utf-8")).hexdigest()[:16]


def hash_content(content: str) -> str:
    """Hash raw content string (SHA-256 truncated to 16 hex chars)."""
    return hashlib.sha256(content.strip().encode("utf-8")).hexdigest()[:16]
