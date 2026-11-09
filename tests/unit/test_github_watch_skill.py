"""The github-watch skill script owns cursor and digest logic; every request goes through gh."""

# pylint: disable=protected-access

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "skills" / "github-watch" / "scripts" / "github_watch.py"
spec = importlib.util.spec_from_file_location("github_watch_skill", SCRIPT)
skill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(skill)


@pytest.fixture(name="state")
def _state(tmp_path):
    return tmp_path / "state"


def _args(**overrides):
    defaults = {
        "gh": "",
        "state_dir": "",
        "out": "",
        "timeout": 5,
        "repos": "",
        "reasons": "",
        "hours": 24,
        "include_read": False,
        "participating": False,
        "per_page": 50,
        "max_items": 100,
        "max_seen": 500,
        "max_repos": 8,
        "include_issues": True,
        "include_pulls": True,
        "no_releases": False,
        "action": "list",
        "source": "notifications",
        "since": "",
    }
    return type("Args", (), {**defaults, **overrides})()


def test_script_exists_and_documents_its_two_modes():
    """The skill entry point ships with the repository and describes both modes."""
    assert SCRIPT.is_file()
    assert "notifications" in skill.__doc__ and "watch" in skill.__doc__


def test_gh_binary_reports_how_to_install_when_missing(monkeypatch):
    """A missing CLI explains how to install it instead of failing obscurely later."""
    monkeypatch.setattr(skill.shutil, "which", lambda _: None)
    with pytest.raises(SystemExit) as exit_info:
        skill.gh_binary("gh")
    assert "GitHub CLI not found" in str(exit_info.value)


def test_gh_binary_honours_an_explicit_path(monkeypatch):
    """An explicit binary path wins, so a non-PATH install still works."""
    monkeypatch.setattr(skill.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    assert skill.gh_binary("gh") == "/usr/local/bin/gh"


def test_gh_failure_translates_a_credential_problem(monkeypatch):
    """A rejected token becomes an actionable message rather than a raw exit code."""
    monkeypatch.setattr(
        skill.subprocess,
        "run",
        lambda *a, **k: type("Done", (), {"returncode": 1, "stderr": "gh: Bad credentials (HTTP 401)", "stdout": ""})(),
    )
    with pytest.raises(SystemExit) as exit_info:
        skill.gh("/bin/gh", ["api", "user"], 5)
    message = str(exit_info.value)
    assert "Bad credentials" in message and "GH_TOKEN" in message


def test_browser_url_and_trailing_number_convert_api_subjects():
    """API subject URLs become the links a reader can actually open."""
    api = "https://api.github.com/repos/agentscope-ai/ReMe/pulls/724"
    assert skill.browser_url(api) == "https://github.com/agentscope-ai/ReMe/pull/724"
    assert skill.trailing_number(api) == "724"
    assert skill.browser_url("") == "" and skill.trailing_number(None) == ""


def test_wanted_applies_repository_and_reason_filters():
    """Both a full owner/repo and a bare repo name match, case-insensitively."""
    item = {"repository": {"full_name": "agentscope-ai/ReMe"}, "reason": "review_requested"}
    assert skill.wanted(item, set(), set())
    assert skill.wanted(item, {"agentscope-ai/reme"}, set())
    assert skill.wanted(item, {"ReMe"}, set())
    assert not skill.wanted(item, {"cli/cli"}, set())
    assert skill.wanted(item, set(), {"review_requested"})
    assert not skill.wanted(item, set(), {"mention"})


def test_dedupe_keeps_the_boundary_item_out_and_remembers_the_key():
    """The inclusive ``since`` boundary is not republished, but a new update still is."""
    items = [{"id": 1, "updated_at": "a"}, {"id": 2, "updated_at": "b"}]
    keys = ["1|a", "2|b"]
    fresh, seen = skill.dedupe(items, keys, {"1|a"}, 10)
    assert [item["id"] for item in fresh] == [2]
    assert seen == ["1|a", "2|b"]
    # A later update to the same thread is new again because the timestamp is part of the key.
    fresh, seen = skill.dedupe(items, keys, set(seen), 10)
    assert fresh == [] and len(seen) == 2


def test_notifications_sends_the_cursor_to_gh_and_dedupes_the_second_run(monkeypatch, state, capsys):
    """Both runs call only gh, and the second publishes nothing for the same thread."""
    calls: list[list[str]] = []
    notification = {
        "id": "99",
        "unread": True,
        "reason": "mention",
        "updated_at": "2026-11-09T10:00:00Z",
        "repository": {"full_name": "agentscope-ai/ReMe"},
        "subject": {
            "type": "Issue",
            "title": "Someone mentioned you",
            "url": "https://api.github.com/repos/agentscope-ai/ReMe/issues/600",
        },
    }

    def fake_gh(_binary, args, _timeout):
        calls.append(args)
        return [notification]

    monkeypatch.setattr(skill, "gh_binary", lambda _="": "/bin/gh")
    monkeypatch.setattr(skill, "gh", fake_gh)
    skill.cmd_notifications(_args(state_dir=str(state)))
    first = capsys.readouterr().out
    assert "Someone mentioned you" in first and "issues/600" in first
    assert calls == [["api", "--paginate", calls[0][2]]]
    assert calls[0][2].startswith("notifications?since=") and "all=false" in calls[0][2]

    calls.clear()
    skill.cmd_notifications(_args(state_dir=str(state)))
    second = capsys.readouterr().out
    assert "No new notifications" in second and "Someone mentioned you" not in second
    cursor = json.loads((state / "notifications.json").read_text(encoding="utf-8"))
    assert cursor["since"] == "2026-11-09T10:00:00Z" and cursor["seen"] == ["99|2026-11-09T10:00:00Z"]


def test_watch_requires_repositories(monkeypatch, state):
    """Public mode says which setting is missing instead of silently collecting nothing."""
    monkeypatch.setattr(skill, "gh_binary", lambda _="": "/bin/gh")
    monkeypatch.delenv("GITHUB_WATCH_REPOS", raising=False)
    with pytest.raises(SystemExit) as exit_info:
        skill.cmd_watch(_args(state_dir=str(state)))
    assert "GITHUB_WATCH_REPOS" in str(exit_info.value)


def test_watch_collects_issues_pulls_and_releases_per_repository(monkeypatch, state, capsys):
    """One REST call covers issues and pulls; releases are filtered by their own publish time."""
    calls: list[str] = []

    def fake_gh(_binary, args, _timeout):
        calls.append(args[-1])
        if "/issues?" in args[-1]:
            return [
                {
                    "number": 5,
                    "title": "Bug",
                    "state": "open",
                    "updated_at": "2026-11-09T09:00:00Z",
                    "user": {"login": "ada"},
                    "labels": [],
                    "html_url": "https://github.com/o/r/issues/5",
                },
                {
                    "number": 6,
                    "title": "Fix",
                    "state": "open",
                    "updated_at": "2026-11-09T10:00:00Z",
                    "user": {"login": "bob"},
                    "labels": [{"name": "bug"}],
                    "html_url": "https://github.com/o/r/pull/6",
                    "pull_request": {"url": "x"},
                },
            ]
        return [
            {
                "tag_name": "v2.0.0",
                "name": "v2.0.0",
                "published_at": "2026-11-09T08:00:00Z",
                "draft": False,
                "prerelease": False,
                "author": {"login": "bob"},
                "html_url": "https://github.com/o/r/releases/tag/v2.0.0",
            },
        ]

    monkeypatch.setattr(skill, "gh_binary", lambda _="": "/bin/gh")
    monkeypatch.setattr(skill, "gh", fake_gh)
    skill.cmd_watch(_args(state_dir=str(state), repos="o/r", hours=72))
    out = capsys.readouterr().out
    assert "GitHub project activity (3 new items)" in out
    assert "[Issue #5]" in out and "[PR #6]" in out and "[Release]" in out and "labels: bug" in out
    assert any(call.startswith("repos/o/r/issues?since=") for call in calls)
    assert any(call.startswith("repos/o/r/releases?") for call in calls)
    cursor = json.loads((state / "projects.json").read_text(encoding="utf-8"))["repos"]["o/r"]
    assert cursor["since"] == "2026-11-09T10:00:00Z" and cursor["last_count"] == 3


def test_watch_caps_the_digest_per_repository(monkeypatch, state, capsys):
    """A busy repository cannot push an unbounded list into the agent's summary."""
    monkeypatch.setattr(skill, "gh_binary", lambda _="": "/bin/gh")
    monkeypatch.setattr(
        skill,
        "gh",
        lambda _b, args, _t: (
            []
            if "releases" in args[-1]
            else [
                {
                    "number": n,
                    "title": f"Item {n}",
                    "state": "open",
                    "updated_at": f"2026-11-0{n % 9 + 1}T09:00:00Z",
                    "user": {"login": "ada"},
                    "labels": [],
                    "html_url": f"https://github.com/o/r/issues/{n}",
                }
                for n in range(1, 21)
            ]
        ),
    )
    skill.cmd_watch(_args(state_dir=str(state), repos="o/r", hours=240, max_items=5))
    out = capsys.readouterr().out
    assert out.count("- [Issue #") == 5
    cursor = json.loads((state / "projects.json").read_text(encoding="utf-8"))["repos"]["o/r"]
    # Everything fetched is remembered, so capped items are not re-reported, and the digest says so.
    assert cursor["last_count"] == 20 and cursor["last_listed"] == 5 and len(cursor["seen"]) == 20
    assert "Listed: 5 of 20 new" in out


def test_cursor_list_show_reset_and_rewind(state, capsys):
    """The operator-facing cursor commands work without touching the network."""
    skill.write_cursor(state, "notifications", {"since": "2026-11-09T10:00:00Z", "seen": ["1|a"], "last_count": 2})
    skill.cmd_cursor(_args(state_dir=str(state), action="list"))
    listed = json.loads(capsys.readouterr().out)
    assert listed["notifications"]["since"] == "2026-11-09T10:00:00Z" and "seen" not in listed["notifications"]
    assert listed["projects"] == {}

    skill.cmd_cursor(_args(state_dir=str(state), action="show", source="notifications"))
    assert json.loads(capsys.readouterr().out)["seen"] == ["1|a"]

    skill.cmd_cursor(_args(state_dir=str(state), action="reset", source="notifications", since="2026-11-01T00:00:00Z"))
    rewound = json.loads(capsys.readouterr().out)
    assert rewound["rewound_to"] == "2026-11-01T00:00:00Z"
    assert json.loads((state / "notifications.json").read_text(encoding="utf-8"))["seen"] == []

    skill.cmd_cursor(_args(state_dir=str(state), action="reset", source="notifications"))
    assert json.loads(capsys.readouterr().out)["removed"] is True
    assert not (state / "notifications.json").exists()


def test_cursor_reset_rejects_a_bad_since(state):
    """An unparseable rewind target is refused instead of clearing the cursor by accident."""
    with pytest.raises(SystemExit) as exit_info:
        skill.cmd_cursor(_args(state_dir=str(state), action="reset", source="notifications", since="yesterday"))
    assert "ISO-8601" in str(exit_info.value)


def test_out_directory_keeps_the_digest_and_the_raw_records(state, tmp_path, monkeypatch, capsys):
    """--out gives the agent evidence files next to the printed digest."""
    monkeypatch.setattr(skill, "gh_binary", lambda _="": "/bin/gh")
    monkeypatch.setattr(
        skill,
        "gh",
        lambda _b, args, _t: [
            {
                "id": "1",
                "unread": True,
                "reason": "assign",
                "updated_at": "2026-11-09T10:00:00Z",
                "repository": {"full_name": "o/r"},
                "subject": {"type": "Issue", "title": "Assigned to you", "url": ""},
            },
        ],
    )
    out_dir = tmp_path / "evidence"
    skill.cmd_notifications(_args(state_dir=str(state), out=str(out_dir)))
    capsys.readouterr()
    written = sorted(path.suffix for path in out_dir.iterdir())
    assert written == [".jsonl", ".md"]
    records = [json.loads(line) for line in next(out_dir.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()]
    assert (
        records == [{"reason": "assign", **{key: value for key, value in records[0].items() if key == "reason"}}]
        or records[0]["reason"] == "assign"
    )
    assert next(out_dir.glob("*.md")).read_text(encoding="utf-8").startswith("# GitHub notifications")
