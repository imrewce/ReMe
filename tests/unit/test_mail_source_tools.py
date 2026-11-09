"""IMAP mail tools and the shared cursor, driven by a fake server instead of a network."""

# pylint: disable=protected-access,unused-argument

import email.message
from email.utils import formatdate
import json

import pytest

from reme.components.application_context import ApplicationContext
from reme.steps.proactive.data_connectors import imap_mail as mail_module
from reme.steps.proactive.data_connectors import MailAuthStep, MailFetchStep, SourceCursorStep
from reme.steps.proactive.data_connectors._remote import Cursor, MissingCredentials


def _raw(subject: str, sender: str, body: str = "", html: bool = False) -> bytes:
    """Build one RFC822 message, optionally HTML with a named attachment."""
    message = email.message.EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = "me@example.com"
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = f"<{abs(hash(subject))}@example.com>"
    if html:
        message.set_content(f"<html><body><p>{body}</p></body></html>", subtype="html")
        message.add_attachment(b"PDFDATA", maintype="application", subtype="pdf", filename="report.pdf")
    else:
        message.set_content(body)
    return message.as_bytes()


class FakeIMAP:
    """The imaplib surface the mail steps use, including bare-value UIDVALIDITY reporting."""

    def __init__(self, store, host, port, timeout=None):
        self.store = store
        self.host, self.port, self.timeout = host, port, timeout
        self.logins: list[tuple[str, str]] = []
        self.logged_out = 0

    def login(self, user, password):
        """Record the attempt and reject anything but the configured authorization code."""
        self.store["logins"].append((user, password))
        if password != "client-auth-code":
            raise FakeIMAP.error("LOGIN authentication error")
        return "OK", [b"LOGIN completed"]

    def capability(self):
        """Answer the post-login capability probe."""
        return "OK", ("IMAP4REV1", "ID")

    def list(self):
        """Answer LIST with quoted folder names, including one containing a space."""
        return "OK", [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren) "/" "Sent Messages"',
            b'(\\Noselect) "/" "Folders"',
        ]

    def select(self, mailbox, readonly=False):
        """Select one mailbox, or refuse a name the server does not have."""
        if mailbox not in self.store["boxes"]:
            return "NO", [b"Mailbox not found"]
        self.store["selected"] = mailbox
        return "OK", [str(len(self.store["boxes"][mailbox])).encode()]

    def response(self, code):
        """Report an untagged response the way imaplib does: the bare value only."""
        # imaplib reports the bare value; a server may echo the full tag instead.
        return code.upper(), [str(self.store["uidvalidity"]).encode()]

    def uid(self, command, *args):
        """Answer UID SEARCH and UID FETCH, including the ``N:*`` last-message quirk."""
        box = self.store["boxes"][self.store["selected"]]
        if command == "SEARCH":
            criteria = list(args)
            self.store["searches"].append(criteria)
            if "UID" in criteria:
                low = int(criteria[criteria.index("UID") + 1].split(":")[0])
                # A real server answers "N:*" with the last message even below N.
                uids = sorted(uid for uid in box if uid >= low) or ([max(box)] if box else [])
            else:
                uids = sorted(box)
            return "OK", [" ".join(str(uid) for uid in uids).encode()]
        if args[-1] == "(RFC822.SIZE)":
            return "OK", [f"{uid} (UID {uid} RFC822.SIZE {len(box[uid])})".encode() for uid in sorted(box)]
        uid = int(args[0])
        if uid not in box:
            return "NO", [b"fetch failed"]
        return "OK", [(f"{uid} (BODY[] {{{len(box[uid])}}}".encode(), box[uid]), b")"]

    def logout(self):
        """Count disconnects so a test can assert the connection was released."""
        self.logged_out += 1

    class error(Exception):
        """Stand in for imaplib.IMAP4.error."""


@pytest.fixture(name="server")
def _server(tmp_path, monkeypatch):
    """One workspace, one fake server, and credentials supplied through the environment."""
    store = {
        "boxes": {"INBOX": {5: _raw("Re: PR review", "boss@example.com", "Please review before Friday.")}},
        "uidvalidity": 4242,
        "selected": None,
        "searches": [],
        "logins": [],
    }
    monkeypatch.setenv("IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("IMAP_USER", "me@example.com")
    monkeypatch.setenv("IMAP_PASSWORD", "client-auth-code")

    # Patch only the transport so the real credential and login path stays under test.
    monkeypatch.setattr(mail_module, "IMAP4_SSL", lambda host, port, timeout=None: FakeIMAP(store, host, port, timeout))
    app = ApplicationContext(workspace_dir=str(tmp_path), metadata_dir="reme_metadata")
    return {"store": store, "app": app, "tmp": tmp_path, "cursor": tmp_path / "reme_metadata" / "step" / "mail.json"}


def _read_jsonl(tmp_path, paths):
    payload = next(path for path in paths if path.endswith(".jsonl"))
    return [json.loads(line) for line in (tmp_path / payload).read_text(encoding="utf-8").splitlines()]


@pytest.mark.asyncio
async def test_auth_check_reports_capabilities_and_verbatim_folder_names(server):
    """Folder names with spaces survive, so they can be pasted into IMAP_MAILBOXES."""
    response = await MailAuthStep(app_context=server["app"])()
    context = response.answer["context"]
    assert response.answer["paths"] == []
    assert "credentials accepted" in context
    assert "Sent Messages" in context
    assert "INBOX (1 messages)" in context
    assert "IMAP4REV1" in context


@pytest.mark.asyncio
async def test_first_fetch_publishes_digest_and_records_and_stores_the_uid_cursor(server):
    """One package holds the Markdown digest and its JSONL evidence, and the cursor advances."""
    response = await MailFetchStep(app_context=server["app"])()
    paths = response.answer["paths"]
    assert [path.split("/")[-1].split("_")[-1] for path in paths] == ["mail.md", "mail.jsonl"]
    assert all(path.startswith("resource/mail/") for path in paths)
    digest = (server["tmp"] / paths[0]).read_text(encoding="utf-8")
    assert "Re: PR review" in digest and "boss@example.com" in digest
    records = _read_jsonl(server["tmp"], paths)
    assert records[0]["uid"] == 5 and records[0]["mailbox"] == "INBOX"
    stored = json.loads(server["cursor"].read_text(encoding="utf-8"))
    assert stored["mailboxes"]["INBOX"] == {"uidvalidity": "4242", "last_uid": 5, "messages": 1}


@pytest.mark.asyncio
async def test_refetch_without_new_mail_publishes_nothing_and_keeps_the_cursor(server):
    """The boundary message a real server returns for ``N:*`` must not be republished."""
    await MailFetchStep(app_context=server["app"])()
    response = await MailFetchStep(app_context=server["app"])()
    assert response.answer["paths"] == []
    assert "No new messages" in response.answer["context"]
    assert server["store"]["searches"][-1] == ["UID", "6:*"]


@pytest.mark.asyncio
async def test_only_messages_above_the_cursor_are_collected(server):
    """A later arrival is collected alone, and the search is UID-based rather than date-based."""
    await MailFetchStep(app_context=server["app"])()
    server["store"]["boxes"]["INBOX"][7] = _raw("Urgent: incident", "ops@example.com", "Confirm the failover.")
    response = await MailFetchStep(app_context=server["app"])()
    records = _read_jsonl(server["tmp"], response.answer["paths"])
    assert [record["uid"] for record in records] == [7]
    assert server["store"]["searches"][-1] == ["UID", "6:*"]
    assert json.loads(server["cursor"].read_text(encoding="utf-8"))["mailboxes"]["INBOX"]["last_uid"] == 7


@pytest.mark.asyncio
async def test_changed_uidvalidity_restarts_the_bounded_window(server):
    """A rebuilt mailbox invalidates UIDs, so the stale cursor must not be trusted."""
    await MailFetchStep(app_context=server["app"])()
    server["store"]["uidvalidity"] = 9999
    server["store"]["boxes"]["INBOX"] = {1: _raw("After rebuild", "new@example.com", "Fresh mailbox.")}
    response = await MailFetchStep(app_context=server["app"])()
    assert [record["uid"] for record in _read_jsonl(server["tmp"], response.answer["paths"])] == [1]
    assert server["store"]["searches"][-1][0] == "SINCE"


@pytest.mark.asyncio
async def test_html_body_is_flattened_and_attachments_are_named_not_stored(server):
    """HTML becomes readable text and an attachment contributes only its name."""
    server["store"]["boxes"]["INBOX"][6] = _raw("Release notes", "noreply@example.com", "please review", html=True)
    response = await MailFetchStep(app_context=server["app"])()
    records = {record["uid"]: record for record in _read_jsonl(server["tmp"], response.answer["paths"])}
    assert records[6]["body"] == "please review"
    assert records[6]["attachments"] == "report.pdf (application/pdf)"
    assert "PDFDATA" not in (server["tmp"] / response.answer["paths"][0]).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_message_budget_defers_the_remainder_to_the_next_poll(server):
    """Oldest first, so a truncated backlog continues instead of being skipped."""
    for uid in (6, 7, 8):
        server["store"]["boxes"]["INBOX"][uid] = _raw(f"Message {uid}", f"sender{uid}@example.com", "body")
    response = await MailFetchStep(app_context=server["app"], max_messages=2)()
    assert [record["uid"] for record in _read_jsonl(server["tmp"], response.answer["paths"])] == [5, 6]
    assert json.loads(server["cursor"].read_text(encoding="utf-8"))["mailboxes"]["INBOX"]["last_uid"] == 6
    response = await MailFetchStep(app_context=server["app"], max_messages=2)()
    assert [record["uid"] for record in _read_jsonl(server["tmp"], response.answer["paths"])] == [7, 8]


@pytest.mark.asyncio
async def test_unread_only_search_is_opt_in(server):
    """The default search is UID-based; UNSEEN is only added when asked for."""
    await MailFetchStep(app_context=server["app"], unseen_only=True)()
    assert server["store"]["searches"][-1][0] == "UNSEEN"


@pytest.mark.asyncio
async def test_missing_credentials_fail_loudly_before_connecting(server, monkeypatch):
    """A missing credential raises rather than publishing an empty package."""
    monkeypatch.delenv("IMAP_PASSWORD")
    with pytest.raises(MissingCredentials):
        await MailFetchStep(app_context=server["app"])()
    assert not (server["tmp"] / "resource").exists()
    assert server["store"]["searches"] == []


@pytest.mark.asyncio
async def test_unselectable_configured_mailbox_fails_the_poll(server):
    """A wrong folder name surfaces as an error instead of a silent empty result."""
    with pytest.raises(RuntimeError, match="cannot select mailbox"):
        await MailFetchStep(app_context=server["app"], mailboxes="Sent")()


@pytest.mark.asyncio
async def test_cursor_tool_lists_shows_and_resets(server):
    """The cursor tool is the operator's way to inspect and rewind collection state."""
    await MailFetchStep(app_context=server["app"])()
    listed = await SourceCursorStep(app_context=server["app"], action="list")()
    assert listed.answer["sources"]["mail"]["cursor"]["mailboxes"]["INBOX"]["last_uid"] == 5
    shown = await SourceCursorStep(app_context=server["app"], action="show", source="mail")()
    assert shown.answer["cursor"]["mailboxes"]["INBOX"]["uidvalidity"] == "4242"
    reset = await SourceCursorStep(app_context=server["app"], action="reset", source="mail")()
    assert reset.answer["removed"] is True and not server["cursor"].exists()
    response = await MailFetchStep(app_context=server["app"])()
    assert server["store"]["searches"][-1][0] == "SINCE"
    assert len(_read_jsonl(server["tmp"], response.answer["paths"])) == 1


@pytest.mark.asyncio
async def test_cursor_tool_rejects_unknown_source_and_bad_since(server):
    """A stale source name or an unparseable rewind target is refused, not guessed at."""
    with pytest.raises(ValueError, match="unknown source"):
        await SourceCursorStep(app_context=server["app"], action="show", source="github_notifications")()
    with pytest.raises(ValueError, match="ISO-8601"):
        await SourceCursorStep(app_context=server["app"], action="reset", source="mail", since="yesterday")()


def test_cursor_tolerates_a_corrupt_file_and_writes_atomically(tmp_path):
    """A truncated cursor degrades to a first run instead of breaking collection."""
    app = ApplicationContext(workspace_dir=str(tmp_path), metadata_dir="reme_metadata")
    step = MailFetchStep(app_context=app)
    cursor = Cursor(step, "mail")
    cursor.path.parent.mkdir(parents=True, exist_ok=True)
    cursor.path.write_text("{not json", encoding="utf-8")
    assert not cursor.read()
    cursor.write({"mailboxes": {"INBOX": {"last_uid": 3}}})
    assert json.loads(cursor.path.read_text(encoding="utf-8"))["mailboxes"]["INBOX"]["last_uid"] == 3
    assert not list(cursor.path.parent.glob("*.tmp"))
    assert cursor.reset() is True and not cursor.read() and cursor.reset() is False


@pytest.mark.asyncio
async def test_rejected_password_fails_the_poll_and_leaves_the_cursor_alone(server, monkeypatch):
    """A credential the server rejects must surface, and must not skip the unread backlog."""
    await MailFetchStep(app_context=server["app"])()
    assert server["store"]["logins"] == [("me@example.com", "client-auth-code")]
    before = server["cursor"].read_text(encoding="utf-8")
    monkeypatch.setenv("IMAP_PASSWORD", "wrong-secret")
    with pytest.raises(Exception, match="authentication error"):
        await MailFetchStep(app_context=server["app"])()
    assert server["cursor"].read_text(encoding="utf-8") == before
