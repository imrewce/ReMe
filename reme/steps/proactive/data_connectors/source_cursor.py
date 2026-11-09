"""Inspect and reset the incremental cursor that keeps remote source collection deduplicated."""

from ._remote import Cursor, iso, parse_iso
from ...base_step import BaseStep
from ....components import R

SOURCES = {
    "mail": "Personal mailbox messages collected over IMAP, one UID cursor per mailbox.",
}


def _summary(values: dict) -> dict:
    """Reduce one stored cursor to the fields worth showing an agent."""
    if "repos" in values:
        return {
            "polled_at": values.get("polled_at"),
            "repos": {
                repo: {"since": entry.get("since"), "last_count": entry.get("last_count")}
                for repo, entry in (values.get("repos") or {}).items()
            },
        }
    if "mailboxes" in values:
        return {
            "polled_at": values.get("polled_at"),
            "mailboxes": {
                name: {"last_uid": entry.get("last_uid")} for name, entry in (values.get("mailboxes") or {}).items()
            },
        }
    return {key: values.get(key) for key in ("since", "collected_at", "last_count") if key in values}


@R.register("source_cursor_step")
class SourceCursorStep(BaseStep):
    """List every cursor, show one, or reset one so the next collection restarts its window."""

    async def execute(self):
        action = str(self.context.get("action") or self.kwargs.get("action") or "list").strip().lower()
        source = str(self.context.get("source") or self.kwargs.get("source") or "").strip()
        if source and source not in SOURCES:
            raise ValueError(f"unknown source {source!r}; expected one of {', '.join(sorted(SOURCES))}")
        if action == "list":
            answer = {
                "sources": {
                    name: {
                        "description": description,
                        "path": self.to_workspace_relative(Cursor(self, name).path),
                        "cursor": _summary(Cursor(self, name).read()),
                    }
                    for name, description in SOURCES.items()
                },
            }
        elif action == "show":
            if not source:
                raise ValueError("action=show requires a source")
            cursor = Cursor(self, source)
            answer = {"source": source, "path": self.to_workspace_relative(cursor.path), "cursor": cursor.read()}
        elif action == "reset":
            if not source:
                raise ValueError("action=reset requires a source")
            cursor = Cursor(self, source)
            previous = cursor.read()
            removed = cursor.reset()
            raw_since = self.context.get("since") or self.kwargs.get("since")
            since = parse_iso(raw_since)
            if since is None and str(raw_since or "").strip():
                raise ValueError(f"since must be an ISO-8601 timestamp; got {raw_since!r}")
            if since is not None:
                # Rewind instead of clearing, so the next poll re-reads a bounded window.
                cursor.write({"mailboxes": {}})
            answer = {
                "source": source,
                "removed": removed,
                "rewound_to": iso(since) if since is not None else None,
                "previous": _summary(previous) if removed else {},
                "note": (
                    "Next collection restarts from its initial window."
                    if since is None
                    else f"Next collection re-reads from {iso(since)}; stored dedup keys were dropped."
                ),
            }
        else:
            raise ValueError(f"action must be list, show, or reset; got {action!r}")
        self.context.response.answer = answer
        self.context.response.success = True
        return self.context.response
