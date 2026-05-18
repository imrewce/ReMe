"""Read a markdown file from the vault, with line-range slicing and byte-truncation."""

from pathlib import Path

from ._file_io import (
    DEFAULT_MAX_BYTES,
    read_file_safe,
    resolve_path,
    truncate_text_output,
)
from ..base_step import BaseStep
from ...components import R


@R.register("read_step")
class ReadStep(BaseStep):
    """Read a markdown file. Optional `start_line`/`end_line` for ranged reads."""

    def _working_path(self) -> Path:
        if self.app_context is not None:
            return Path(self.app_context.app_config.working_dir).resolve()
        return Path.cwd().resolve()

    def _fail(self, message: str, **meta) -> None:
        assert self.context is not None
        self.context.response.success = False
        self.context.response.answer = f"Error: {message}"
        if meta:
            self.context.response.metadata.update(meta)

    async def execute(self):
        assert self.context is not None
        raw = str(self.context.get("path") or "")
        start_line = self.context.get("start_line")
        end_line = self.context.get("end_line")
        max_bytes = int(self.context.get("max_bytes") or DEFAULT_MAX_BYTES)

        target, err = resolve_path(self._working_path(), raw, require_md=True)
        if err:
            self._fail(err)
            return

        for label, value in (("start_line", start_line), ("end_line", end_line)):
            if value is None:
                continue
            try:
                int(value)
            except (TypeError, ValueError):
                self._fail(f"{label} must be an integer, got {value!r}")
                return

        if not target.exists():
            self._fail(f"file {target} does not exist", path=str(target))
            return
        if not target.is_file():
            self._fail(f"path {target} is not a file", path=str(target))
            return

        try:
            content = await read_file_safe(target)
        except Exception as e:
            self._fail(f"read failed: {e}", path=str(target))
            return

        all_lines = content.split("\n")
        total = len(all_lines)
        s = max(1, int(start_line) if start_line is not None else 1)
        e = min(total, int(end_line) if end_line is not None else total)

        if s > total:
            self._fail(
                f"start_line {s} exceeds file length ({total} lines)",
                path=str(target), total_lines=total,
            )
            return
        if s > e:
            self._fail(f"start_line ({s}) > end_line ({e})", path=str(target))
            return

        selected = "\n".join(all_lines[s - 1: e])
        text = truncate_text_output(
            selected,
            start_line=s,
            total_lines=total,
            max_bytes=max_bytes,
            file_path=str(target),
        )

        self.context.response.success = True
        self.context.response.answer = text
        self.logger.info(
            f"[{self.name}] read path={target} lines={s}-{e}/{total} bytes={len(text.encode('utf-8'))}",
        )
        return self.context.response
