from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


ALLOWED_ATTACHMENT_SUFFIXES = {".txt", ".csv", ".tsv", ".json", ".yaml", ".yml", ".md"}
MAX_ATTACHMENT_BYTES = 256 * 1024
MAX_ATTACHMENT_LINES = 4_000
MAX_PREVIEW_CHARACTERS = 4_000


class AttachmentError(ValueError):
    pass


@dataclass(frozen=True)
class TextAttachment:
    name: str
    suffix: str
    size_bytes: int
    text: str
    preview: str


def load_text_attachment(path: Path) -> TextAttachment:
    if not path.is_file() or path.is_symlink():
        raise AttachmentError("只能添加本地普通文本文件。")
    suffix = path.suffix.lower()
    if suffix not in ALLOWED_ATTACHMENT_SUFFIXES:
        raise AttachmentError("仅支持 TXT、CSV、TSV、JSON、YAML、YML 和 Markdown 文本附件。")
    size_bytes = path.stat().st_size
    if size_bytes > MAX_ATTACHMENT_BYTES:
        raise AttachmentError(f"文本附件不能超过 {MAX_ATTACHMENT_BYTES // 1024} KiB。")
    raw = path.read_bytes()
    if b"\0" in raw:
        raise AttachmentError("附件包含二进制内容，不能作为文本元数据使用。")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise AttachmentError("附件必须采用 UTF-8 编码。") from exc
    if text.count("\n") + 1 > MAX_ATTACHMENT_LINES:
        raise AttachmentError(f"文本附件不能超过 {MAX_ATTACHMENT_LINES} 行。")
    return TextAttachment(path.name, suffix, size_bytes, text, _safe_preview(text))


def _safe_preview(text: str) -> str:
    shown = text[:MAX_PREVIEW_CHARACTERS]
    shown = re.sub(r"(?i)(password|token|api[_ -]?key|secret)\s*[:=]\s*[^\s,;]+", r"\1=<已遮蔽>", shown)
    if len(text) > len(shown):
        shown += "\n…（本地预览已截断）"
    return shown
