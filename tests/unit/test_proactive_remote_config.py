"""The opt-in remote config extends proactive.yaml without changing it."""

from reme.config.config_parser import resolve_app_config

REMOTE_TOOLS = ["mail_fetch", "mail_auth_check", "source_cursor"]


def _config(name: str) -> dict:
    """Resolve one shipped config without touching the network."""
    return resolve_app_config(config=name, log_config=False)


def test_upstream_proactive_config_is_untouched_by_the_remote_sources():
    """The shipped proactive workspace keeps its pinned cron toolset and no credential jobs."""
    cron_tools = _config("proactive")["jobs"]["cron_agent"]["steps"][0]["job_tools"]
    assert cron_tools == ["screen_capture", "finance_news", "get_chat_messages", "send_chat_message_immediate"]
    assert not any(tool in _config("proactive")["jobs"] for tool in REMOTE_TOOLS)


def test_remote_config_registers_the_mail_jobs_and_gives_them_to_the_cron_agent():
    """The mail jobs exist and are actually handed to the agents that collect."""
    config = _config("proactive_remote")
    jobs = config["jobs"]
    assert all(tool in jobs for tool in REMOTE_TOOLS)
    assert jobs["mail_fetch"]["steps"][0]["backend"] == "mail_fetch_step"
    assert jobs["source_cursor"]["steps"][0]["backend"] == "source_cursor_step"
    cron_tools = jobs["cron_agent"]["steps"][0]["job_tools"]
    assert set(REMOTE_TOOLS) <= set(cron_tools)
    # Every tool the agents are handed must resolve to a real job.
    assert set(cron_tools) <= jobs.keys()
    assert set(jobs["temp_proactive"]["steps"][0]["job_tools"]) <= jobs.keys()


def test_remote_config_exposes_the_github_skill_to_the_agent():
    """The cron agent reaches GitHub through the skill, so the wrapper must load it."""
    wrapper = _config("proactive_remote")["components"]["agent_wrapper"]["default"]
    assert wrapper["skills"] == ["github-watch"]
    assert wrapper["project_path"] == ".."
    assert wrapper["backend"] == "codex" and wrapper["sandbox"] == "full-access"


def test_remote_config_inherits_and_extends_the_default_task_schedule():
    """Inherited schedules survive the override, and GitHub is on while mail waits for credentials."""
    tasks = {task["id"]: task for task in _config("proactive_remote")["components"]["pa"]["default"]["default_tasks"]}
    assert {"screen_activity", "finance_news", "daily_fresh_news"} <= tasks.keys()
    assert "github-watch" in tasks["github_inbox"]["query"]
    assert tasks["github_inbox"]["enabled"] is True
    # Mail stays off until credentials exist, so a fresh workspace does not poll and fail.
    assert tasks["mail_inbox"]["enabled"] is False
    assert all(set(task) == {"id", "cron", "query", "enabled"} for task in tasks.values())
