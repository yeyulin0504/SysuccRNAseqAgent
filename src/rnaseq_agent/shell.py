from __future__ import annotations

import shlex


def shell_quote(value: str) -> str:
    return shlex.quote(value)


def remote_path(*parts: str) -> str:
    cleaned = [part.strip("/") for part in parts if part]
    if not cleaned:
        return ""
    prefix = "/" if parts[0].startswith("/") else ""
    return prefix + "/".join(cleaned)
