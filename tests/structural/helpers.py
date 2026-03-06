"""Shared helpers for structural tests."""


def parse_frontmatter(path):
    """Parse simple YAML frontmatter from a markdown file.

    Returns (frontmatter_dict, body_text) or (None, full_text) if no frontmatter.
    The parser intentionally only supports the flat `key: value` form used by
    the plugin SKILL.md files so structural tests do not need extra deps.
    """
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return None, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None, text

    frontmatter = {}
    for raw_line in parts[1].splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        frontmatter[key] = value

    return frontmatter or None, parts[2]
