from __future__ import annotations

import re
from pathlib import PurePath


SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def identifier_error(value: object, field: str) -> str | None:
    text = str(value or "")
    if not text:
        return f"{field} is required."
    if text in {".", ".."} or not SAFE_IDENTIFIER.fullmatch(text):
        return (
            f"{field} must be 1-128 characters and contain only ASCII letters, "
            "digits, '.', '_' or '-'; it cannot be '.' or '..'."
        )
    return None


def relative_filename_error(value: object, field: str) -> str | None:
    text = str(value or "")
    if not text:
        return f"{field} is required."
    path = PurePath(text)
    if path.is_absolute() or text in {".", ".."} or len(path.parts) != 1:
        return f"{field} must be a filename inside samples.local_data_dir, not a path."
    if "/" in text or "\\" in text or "\n" in text or "\r" in text:
        return f"{field} contains a path separator or newline."
    return None
