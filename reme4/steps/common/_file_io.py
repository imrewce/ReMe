"""Shared filesystem helpers for CRUD steps (path resolve, safe read, truncation)."""

import logging
import re
from pathlib import Path

import aiofiles
import aiofiles.os

from ...constants import DEFAULT_MAX_BYTES, MAX_FILE_READ_BYTES, TRUNCATION_NOTICE_MARKER

logger = logging.getLogger(__name__)


def resolve_path(
    working_path: Path,
    raw: str,
    *,
    require_md: bool = True,
) -> tuple[Path | None, str | None]:
    """Resolve a `path=` argument into an absolute Path (qwenpaw-style).

    Rules:
      - absolute path  → used as-is (with `~` expansion);
      - relative path  → joined under ``working_path`` (the reme working_dir, default ``.reme``).
    Markdown gate (when ``require_md=True``):
      - no suffix (e.g. ``My Note``) → auto-append ``.md``;
      - any other non-``.md`` suffix → reject.
    Returns ``(abs_path, None)`` on success, or ``(None, error_message)`` on failure.
    """
    if not raw or not str(raw).strip():
        return None, "`path` is required"
    s = str(raw).strip()
    p = Path(s).expanduser()
    target = p.resolve() if p.is_absolute() else (working_path / p).resolve()
    if require_md:
        if target.suffix == "":
            target = target.with_suffix(".md")
        elif target.suffix.lower() != ".md":
            return None, (
                f"path {s!r} is not a markdown file; this command only supports .md files"
            )
    return target, None


async def read_file_safe(file_path: Path | str, max_bytes: int = MAX_FILE_READ_BYTES) -> str:
    """Read file with utf-8-sig (BOM-tolerant), fallback to errors='ignore'."""
    stat = await aiofiles.os.stat(str(file_path))
    read_size = min(stat.st_size, max_bytes)
    try:
        async with aiofiles.open(str(file_path), "r", encoding="utf-8-sig") as f:
            return await f.read(read_size)
    except UnicodeDecodeError:
        async with aiofiles.open(
            str(file_path), "r", encoding="utf-8-sig", errors="ignore",
        ) as f:
            return await f.read(read_size)


def truncate_text_output(
    text: str,
    *,
    start_line: int = 1,
    total_lines: int = 0,
    max_bytes: int = DEFAULT_MAX_BYTES,
    file_path: str | None = None,
    encoding: str = "utf-8",
) -> str:
    """Truncate text by bytes preserving line integrity; append a continuation notice.

    See qwenpaw `tools/utils.py` for the same semantics. Returns text unchanged when
    it fits within max_bytes, when max_bytes <= 0, or when the last line itself
    exceeds max_bytes (unhandled edge case).
    """
    if not text or max_bytes <= 0:
        return text

    try:
        if TRUNCATION_NOTICE_MARKER in text:
            return _retruncate(text, max_bytes=max_bytes, encoding=encoding)
        return _truncate_fresh(
            text,
            start_line=start_line,
            total_lines=total_lines,
            max_bytes=max_bytes,
            file_path=file_path,
            encoding=encoding,
        )
    except Exception:
        logger.warning("truncate_text_output failed, returning original text", exc_info=True)
        return text


def _truncate_fresh(
    text: str,
    *,
    start_line: int,
    total_lines: int,
    max_bytes: int,
    file_path: str | None,
    encoding: str,
) -> str:
    text_bytes = text.encode(encoding)
    if len(text_bytes) <= max_bytes:
        return text

    truncated = text_bytes[:max_bytes]
    result = truncated.decode(encoding, errors="ignore")
    newline_count = result.count("\n")
    next_line = start_line + max(1, newline_count)

    if next_line <= total_lines:
        read_from = next_line
    elif start_line < total_lines:
        read_from = total_lines
    else:
        return result

    notice = (
        TRUNCATION_NOTICE_MARKER
        + f"\nThe output above was truncated."
        f"\nThe full content is saved to the file and contains {total_lines} lines in total."
        f"\nThis excerpt starts at line {start_line} and covers the next {max_bytes} bytes."
        f"\nIf the current content is not enough, call `read` with file={file_path or ''} "
        f"start_line={read_from} to read more."
    )
    return result + notice


def _retruncate(text: str, *, max_bytes: int, encoding: str) -> str:
    parts = text.split(TRUNCATION_NOTICE_MARKER, 1)
    original_content = parts[0]
    old_notice = parts[1]

    text_bytes = original_content.encode(encoding)
    if len(text_bytes) <= max_bytes + 100:
        return text

    start_match = re.search(r"starts at line (\d+)", old_notice)
    if not start_match:
        return text
    start_line_parsed = int(start_match.group(1))

    truncated_bytes = text_bytes[:max_bytes]
    result = truncated_bytes.decode(encoding, errors="ignore")
    newline_count = result.count("\n")
    next_line = start_line_parsed + max(1, newline_count)

    if not re.search(r"covers the next \d+ bytes", old_notice):
        return text
    new_notice = re.sub(
        r"covers the next \d+ bytes", f"covers the next {max_bytes} bytes", old_notice,
    )
    new_notice = re.sub(
        r"start_line=\d+ to read more", f"start_line={next_line} to read more", new_notice,
    )
    return result + TRUNCATION_NOTICE_MARKER + new_notice


