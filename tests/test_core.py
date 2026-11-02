from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from askthecow.channels import Channel, load_channel
from askthecow.claude_cli import ClaudeError, ClaudeJob, build_command, run_claude
from askthecow.config import Settings
from askthecow.db import Database
from askthecow.gitrepo import GitRepo, truncate_patch
from askthecow.server import create_app
from askthecow.service import AskService, Inbound

from .conftest import commit_file, read_log


def make_repo(settings) -> GitRepo:
    repo = GitRepo(settings.repo_url, settings.branch, settings.mirror_path, settings.worktrees_path, 0)
    repo.ensure()
    repo.fetch(force=True)
    return repo


# ----------------------------------------------------------------------- git


def test_only_master_is_fetched(settings):
    repo = make_repo(settings)
    refs = repo.git("for-each-ref", "--format=%(refname)").split()
    assert refs == ["refs/heads/master"]
    subjects = [m["subject"] for m in repo.commit_meta(repo.rev_list())]
    assert subjects == ["Initial commit", "Add ADC driver, 1 kHz sampling on PA0", "Add over-current limits"]


def test_fetch_sees_new_master_commits(settings, upstream):
    repo = make_repo(settings)
    old = repo.head()
    new = commit_file(upstream, "drivers/adc.c", "#define ADC_SAMPLE_RATE_HZ 2000\n", "ADC to 2 kHz")
    assert repo.fetch(force=True) == new != old


def test_checkout_is_shared_and_cleaned(settings, upstream):
    repo = make_repo(settings)
    head = repo.head()
    with repo.checkout(head) as a, repo.checkout(head) as b:
        assert a == b and (a / "drivers" / "adc.c").exists()
        assert not (a / "secret.txt").exists()
    new = commit_file(upstream, "x.c", "int x;\n", "x")
    repo.fetch(force=True)
    with repo.checkout(new):
        pass
    assert [p.name for p in settings.worktrees_path.iterdir()] == [new]


def test_truncate_patch_keeps_every_file():
    patch = "stat\n" + "\n".join(f"diff --git a/f{i} b/f{i}\n" + "+x\n" * 500 for i in range(5))
    out = truncate_patch(patch, 3000)
    assert len(out) < 4500
    assert all(f"a/f{i}" in out for i in range(5))


# ---------------------------------------------------------------- claude -p


def test_read_only_command_flags(settings, tmp_path):
    job = ClaudeJob.read_only_agent(prompt="q", cwd=tmp_path, model="m", effort="high", max_budget_usd=1.5)
    cmd = build_command(settings, job, tmp_path / "sys.md")
    assert cmd[1] == "-p"
    for flag in ("--restricted", "--strict-mcp-config", "--no-session-persistence"):
        assert flag in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert cmd[cmd.index("--tools") + 1] == "Read,Grep,Glob,Bash"
    assert "Bash(git log:*)" in cmd and "Bash(git * --output*)" in cmd and "Write" in cmd
    assert "Bash" not in cmd[cmd.index("--allowedTools") + 1 : cmd.index("--disallowedTools")]  # no free Bash
    assert cmd[cmd.index("--max-budget-usd") + 1] == "1.5"
    text_job = ClaudeJob(prompt="q", cwd=tmp_path, model="m", system_prompt="s", json_schema={"type": "object"})
    cmd = build_command(settings, text_job, None)
    assert cmd[cmd.index("--tools") + 1] == ""  # no tools at all
    assert "--allowedTools" not in cmd and json.loads(cmd[cmd.index("--json-schema") + 1]) == {"type": "object"}


def test_run_claude_missing_binary(settings, tmp_path):
    settings.claude_bin = str(tmp_path / "nope")
    with pytest.raises(ClaudeError, match="not found"):
        asyncio.run(run_claude(settings, ClaudeJob(prompt="x", cwd=tmp_path, model="m")))


# ----------------------------------------------------------------- knowledge


def test_calibration_then_incremental(settings, upstream, claude_log):
    service = AskService(settings)
    kb, db = service.kb, service.db

    asyncio.run(kb.sync())
    brief = db.latest_brief()
    assert brief["kind"] == "full" and "FULL" in brief["content"]
    assert db.commit_note_count() == 3  # the feature branch commit is not included
    notes = (settings.knowledge_path / "commit-notes.txt").read_text().splitlines()
    assert len(notes) == 3 and "Add over-current limits" in notes[-1] and "topics: ADC PA0" in notes[-1]
    assert "FULL" in (settings.knowledge_path / "brief.md").read_text()
    assert "Group of" in (settings.knowledge_path / "history.md").read_text()

    calls = read_log(claude_log)
    digest = next(c for c in calls if "--json-schema" in c["argv"])
    assert digest["argv"][digest["argv"].index("--tools") + 1] == ""
    brief_call = next(c for c in calls if "Write the repository brief" in c["prompt"])
    assert brief_call["cwd"].startswith(str(settings.worktrees_path))  # runs in a checkout
    assert "--restricted" in brief_call["argv"] and "senior embedded" in brief_call["appended"]

    n = len(read_log(claude_log))
    asyncio.run(kb.sync())  # nothing new: no claude calls
    assert len(read_log(claude_log)) == n

    new = commit_file(upstream, "drivers/adc.c", "#define ADC_SAMPLE_RATE_HZ 2000\n", "ADC to 2 kHz")
    asyncio.run(kb.sync())
    brief = db.latest_brief()
    assert brief["commit_sha"] == new and brief["kind"] == "incremental" and "UPDATED" in brief["content"]
    assert db.commit_note_count() == 4
    update = next(c for c in reversed(read_log(claude_log)) if "Update the repository brief" in c["prompt"])
    assert "drivers/adc.c" in update["prompt"]
    service.close()


# ------------------------------------------------------------------- service


def test_service_ask_and_threads(settings, upstream, claude_log):
    service = AskService(settings)

    async def go():
        events = []

        async def sink(e):
            events.append(e)

        first = await service.ask(
            Inbound(text="ADC rate?", channel="email", user="eli@corp", thread_id="<t1@corp>", title="ADC",
                    instructions="Plain text only."),
            sink,
        )
        commit_file(upstream, "drivers/adc.c", "#define ADC_SAMPLE_RATE_HZ 2000\n", "ADC to 2 kHz")
        second = await service.ask(Inbound(text="And now?", channel="email", user="eli@corp", thread_id="<t1@corp>"))
        other = await service.ask(Inbound(text="Hi", channel="email", user="x", thread_id="<t2@corp>"))
        failed = await service.ask(Inbound(text="PLEASE_FAIL", channel="api"))
        return first, second, other, failed, events

    first, second, other, failed, events = asyncio.run(go())
    assert "ADC_SAMPLE_RATE_HZ 1000" in first.text and not first.error
    assert "ADC_SAMPLE_RATE_HZ 2000" in second.text  # the newest master commit is used
    assert first.conversation_id == second.conversation_id != other.conversation_id
    assert first.commit_sha != second.commit_sha
    chat = service.db.get_chat(first.conversation_id)
    assert (chat["channel"], chat["external_id"], chat["title"]) == ("email", "<t1@corp>", "ADC")

    types = [e["type"] for e in events]
    assert types[0] == "user_message" and types[-1] == "done" and "tool" in types
    streamed = "".join(e["text"] for e in events if e["type"] == "text")
    assert streamed == first.text and "SUBAGENT" not in streamed
    assert first.meta["denials"] and first.meta["tool_calls"][0]["name"] == "Grep"

    answer_calls = [c for c in read_log(claude_log) if "<question" in c["prompt"]]
    assert "Plain text only." in answer_calls[0]["prompt"]
    assert "ADC rate?" in answer_calls[1]["prompt"] and "And now?" in answer_calls[1]["prompt"]  # history
    assert "repository_brief" in answer_calls[0]["appended"]
    assert failed.error and "error_max_budget_usd" in failed.text
    service.close()


# -------------------------------------------------------------------- server


def test_server_chat_flow(settings):
    app = create_app(settings, run_background=False)
    with TestClient(app) as http:
        h = {"X-User": "eli"}
        chat = http.post("/api/chats", json={}, headers=h).json()
        with http.stream("POST", f"/api/chats/{chat['id']}/messages", json={"content": "ADC rate?"}, headers=h) as r:
            events = [json.loads(line[6:]) for line in r.iter_lines() if line.startswith("data: ")]
        assert events[0]["type"] == "user_message" and events[-1]["type"] == "done"
        full = http.get(f"/api/chats/{chat['id']}", headers=h).json()
        assert [m["role"] for m in full["messages"]] == ["user", "assistant"]
        assert full["title"] == "ADC rate?"
        assert full["messages"][1]["commit_sha"] and "ADC_SAMPLE_RATE_HZ" in full["messages"][1]["content"]
        assert http.get(f"/api/chats/{chat['id']}", headers={"X-User": "mallory"}).status_code == 404
        assert http.get("/api/chats", headers={"X-User": "mallory"}).json() == []
        assert http.get("/api/status").json()["branch"] == "master"
        assert http.post("/api/admin/calibrate").status_code == 403
        assert http.post("/api/ask", json={"text": "x"}).status_code == 403  # off without a token
        assert http.delete(f"/api/chats/{chat['id']}", headers=h).json() == {"ok": True}


def test_json_ask_api(settings):
    settings.api_token = "tok"
    settings.password = "pw"  # Basic auth does not apply to /api/ask; the token does
    app = create_app(settings, run_background=False)
    with TestClient(app) as http:
        body = {"text": "ADC rate?", "channel": "email", "user": "eli@corp", "thread_id": "<a@b>"}
        assert http.post("/api/ask", json=body, headers={"X-Api-Token": "bad"}).status_code == 403
        r = http.post("/api/ask", json=body, headers={"X-Api-Token": "tok"}).json()
        assert not r["error"] and "ADC_SAMPLE_RATE_HZ" in r["text"] and r["commit_sha"]
        r2 = http.post("/api/ask", json={**body, "text": "more"}, headers={"X-Api-Token": "tok"}).json()
        assert r2["conversation_id"] == r["conversation_id"]
        # Email conversations are not shown in the web chat list.
        assert http.get("/api/chats", auth=("eli@corp", "pw")).json() == []


def test_password_and_webhook(settings):
    settings.password = "pw"
    settings.webhook_secret = "s3cret"
    app = create_app(settings, run_background=False)
    with TestClient(app) as http:
        assert http.get("/api/chats").status_code == 401
        assert http.get("/api/chats", auth=("eli", "pw")).status_code == 200
        assert http.post("/api/webhook", json={"ref": "refs/heads/master"}).status_code == 403
        r = http.post("/api/webhook", json={"ref": "refs/heads/dev"}, headers={"X-Gitlab-Token": "s3cret"})
        assert r.json()["ignored"] == "refs/heads/dev"
        body = json.dumps({"ref": "refs/heads/master"}).encode()
        sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        r = http.post("/api/webhook", content=body, headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})
        assert r.json() == {"ok": True}


# ------------------------------------------------------------------ channels


class EchoChannel(Channel):
    """Test channel: asks one question and keeps the answer."""

    name = "echo"
    answers: list = []

    async def run(self) -> None:
        out = await self.service.ask(Inbound(text="ADC rate?", channel=self.name, user="bot", thread_id="1"))
        EchoChannel.answers.append(out)


def test_channel_plugin(settings):
    settings.channels = ["tests.test_core:EchoChannel"]
    settings.poll_interval_seconds = 3600
    EchoChannel.answers.clear()
    app = create_app(settings)
    with TestClient(app):
        for _ in range(200):
            if EchoChannel.answers:
                break
            asyncio.run(asyncio.sleep(0.05))
    assert EchoChannel.answers and "ADC_SAMPLE_RATE_HZ" in EchoChannel.answers[0].text
    with pytest.raises(TypeError):
        load_channel("askthecow.db:Database", None, settings)  # type: ignore[arg-type]


def test_settings_from_env():
    s = Settings.from_env(
        {"ASKTHECOW_CHANNELS": "a.b:C, d.e:F", "ASKTHECOW_AUTO_CALIBRATE": "false", "ASKTHECOW_PORT": "9000",
         "ASKTHECOW_ANSWER_MAX_BUDGET_USD": "2.5", "ASKTHECOW_DATA_DIR": "/x"}
    )
    assert s.channels == ["a.b:C", "d.e:F"] and s.auto_calibrate is False and s.port == 9000
    assert s.answer_max_budget_usd == 2.5 and str(s.data_dir) == "/x"
    assert Database(":memory:").find_chat("email", "nope") is None
