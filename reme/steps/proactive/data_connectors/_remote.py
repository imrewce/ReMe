"""Credentials and the incremental cursor shared by the remote source tools."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from ...base_step import BaseStep


class MissingCredentials(RuntimeError):
    """A required credential is absent or rejected, so the source cannot be collected."""


# ----- settings --------------------------------------------------------------------------------


def setting(step: BaseStep, key: str, env: str = "", default=None):
    """Resolve one option from the tool call, then the step config, then the environment."""
    value = step.context.get(key) if step.context is not None else None
    if value in (None, ""):
        value = step.kwargs.get(key)
    if value in (None, "") and env:
        value = os.environ.get(env)
    return default if value in (None, "") else value


def int_setting(step: BaseStep, key: str, default: int, env: str = "", minimum: int = 1) -> int:
    """Return one integer option clamped to a usable lower bound."""
    return max(minimum, int(setting(step, key, env, default)))


def bool_setting(step: BaseStep, key: str, default: bool = False, env: str = "") -> bool:
    """Return one boolean option, accepting the strings a YAML or CLI override produces."""
    value = setting(step, key, env, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def list_setting(step: BaseStep, key: str, env: str = "") -> list[str]:
    """Accept a YAML list or one comma/space separated string of names."""
    value = setting(step, key, env, "")
    items = [str(item) for item in value] if isinstance(value, (list, tuple)) else str(value).replace(",", " ").split()
    return [item.strip() for item in items if item.strip()]


# ----- time ------------------------------------------------------------------------------------


def iso(moment: datetime) -> str:
    """Render one aware instant in the ``...Z`` form remote cursors expect."""
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value) -> datetime | None:
    """Parse an ISO-8601 cursor into an aware datetime, or ``None`` when unusable."""
    try:
        moment = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


# ----- cursor ----------------------------------------------------------------------------------


class Cursor:
    """One JSON cursor file per source under ``<metadata_dir>/step/``, written atomically.

    The cursor is what makes a source incremental: a tool advances it only after its own
    collection succeeded, so a failed poll repeats instead of silently skipping messages.
    """

    def __init__(self, step: BaseStep, name: str):
        self.name = name
        self.path: Path = step.workspace_path / str(step.config_value("metadata_dir")) / "step" / f"{name}.json"
        self._values: dict | None = None

    def read(self) -> dict:
        """Return the persisted cursor, or an empty mapping on the first run."""
        if self._values is None:
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8")) if self.path.is_file() else {}
            except (OSError, ValueError):
                loaded = {}
            self._values = loaded if isinstance(loaded, dict) else {}
        return dict(self._values)

    def write(self, values: dict) -> dict:
        """Merge and persist cursor fields; returns the stored mapping."""
        merged = {**self.read(), **values}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(merged, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        temporary.replace(self.path)
        self._values = merged
        return dict(merged)

    def reset(self) -> bool:
        """Drop the stored cursor so the next collection restarts from its initial window."""
        self._values = {}
        if not self.path.is_file():
            return False
        self.path.unlink()
        return True
