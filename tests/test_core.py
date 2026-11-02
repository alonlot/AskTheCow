from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from askthecow.agent import run_agent
from askthecow.db import Database
from askthecow.gitrepo import GitRepo, truncate_patch
from askthecow.knowledge import KnowledgeBase
from askthecow.server import create_app
from askthecow.tools import RepoTools, ToolError

from .conftest import FakeClient, commit_file


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
    assert "Feature branch work" not in subjects


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
    # The old head checkout is gone; only the current head stays.
    assert [p.name for p in settings.worktrees_path.iterdir()] == [new]


def test_truncate_patch_keeps_every_file():
    patch = "stat\n" + "\n".join(f"diff --git a/f{i} b/f{i}\n" + "+x\n" * 500 for i in range(5))
    out = truncate_patch(patch, 3000)
    assert len(out) < 4500
    assert all(f"a/f{i}" in out for i in range(5))


# --------------------------------------------------------------------- tools


def test_tools(settings):
    repo = make_repo(settings)
    db = Database(":memory:")
    with repo.checkout(repo.head()) as path:
        t = RepoTools(path, db)
        assert "drivers/" in t.run("list_dir", {"path": "."})
        assert t.run("find_files", {"pattern": "*adc*"}) == "drivers/adc.c"
        assert "2  #define ADC_SAMPLE_RATE_HZ 1000" in t.run("read_file", {"path": "drivers/adc.c"})
        assert "drivers/adc.c:2:" in t.run("search_code", {"pattern": "SAMPLE_RATE", "path_glob": "*.c"})
        assert t.run("search_code", {"pattern": "NOT_THERE_AT_ALL"}) == "No matches."
        log = t.run("git_log", {"path": "config"})
        assert "over-current" in log
        sha = log.split()[0]
        assert "OVERCURRENT_LIMIT_MA 2500" in t.run("git_show", {"commit": sha})
        for bad in ({"path": "../../etc/passwd"}, {"path": ".git/config"}, {"path": "/etc/passwd"}):
            with pytest.raises(ToolError):
                t.run("read_file", bad)
        with pytest.raises(ToolError):
            t.run("read_file", {"path": "README.md", "evil": 1})
        with pytest.raises(ToolError):
            t.run("git_show", {"commit": "HEAD; rm -rf /"})


def test_history_search():
    db = Database(":memory:")
    db.save_digest(
        {"first_sha": "a" * 40, "last_sha": "a" * 40, "first_date": "2024-01-01", "last_date": "2024-01-01", "commit_count": 1, "summary": "s"},
        [{"sha": "a" * 40, "author": "Dana", "date": "2024-01-01", "subject": "PWM", "note": "Fan PWM moved to 25 kHz", "files": "drivers/pwm.c", "topics": "PWM fan TIM3"}],
    )
    assert db.search_commit_notes("fan frequency")[0]["sha"] == "a" * 40
    assert db.search_commit_notes("???") == []


# --------------------------------------------------------------------- agent


def test_agent_loop_runs_tools_and_appends(settings):
    repo = make_repo(settings)
    client = FakeClient()

    async def go():
        events = []

        async def sink(e):
            events.append(e)

        async with repo.acheckout(repo.head()) as path:
            res = await run_agent(
                client, settings, system=[{"type": "text", "text": "sys"}],
                messages=[{"role": "user", "content": "What is the ADC rate?"}],
                tools=RepoTools(path), on_event=sink,
            )
        return res, events

    res, events = asyncio.run(go())
    assert "1 kHz" in res.text and res.turns == 2
    assert [c["name"] for c in res.tool_calls] == ["search_code", "read_file"]
    assert res.tool_calls[1]["error"] is True  # path escape refused
    second = client.calls[1]
    assert second["fallbacks"] == "default" and second["cache_control"] == {"type": "ephemeral"}
    # Assistant content (thinking block included) is kept unchanged, results come in one user message.
    assert second["messages"][1]["content"][0].type == "thinking"
    assert [r["tool_use_id"] for r in second["messages"][2]["content"]] == ["tu_q1", "tu_q2"]
    assert any(e["type"] == "tool" for e in events)


# ----------------------------------------------------------------- knowledge


def test_calibration_then_incremental(settings, upstream):
    db = Database(settings.db_path)
    repo = GitRepo(settings.repo_url, settings.branch, settings.mirror_path, settings.worktrees_path, 0)
    client = FakeClient()
    kb = KnowledgeBase(settings, db, repo, client)

    asyncio.run(kb.sync())
    brief = db.latest_brief()
    assert brief["kind"] == "full" and "FULL" in brief["content"]
    assert db.commit_note_count() == 3  # feature branch commit is not included
    assert db.search_commit_notes("ADC")

    asyncio.run(kb.sync())  # nothing new: no model calls
    calls = len(client.calls)
    asyncio.run(kb.sync())
    assert len(client.calls) == calls

    new = commit_file(upstream, "drivers/adc.c", "#define ADC_SAMPLE_RATE_HZ 2000\n", "ADC to 2 kHz")
    asyncio.run(kb.sync())
    brief = db.latest_brief()
    assert brief["commit_sha"] == new and brief["kind"] == "incremental" and "UPDATED" in brief["content"]
    assert db.commit_note_count() == 4
    update_task = next(c for c in reversed(client.calls) if "Update the repository brief" in str(c["messages"][0]["content"]))
    assert "drivers/adc.c" in update_task["messages"][0]["content"]


# -------------------------------------------------------------------- server


def test_server_chat_flow(settings):
    client = FakeClient()
    app = create_app(settings, client=client, run_sync_loop=False)
    with TestClient(app) as http:
        h = {"X-User": "eli"}
        chat = http.post("/api/chats", json={}, headers=h).json()
        with http.stream("POST", f"/api/chats/{chat['id']}/messages", json={"content": "ADC rate?"}, headers=h) as r:
            events = [json.loads(line[6:]) for line in r.iter_lines() if line.startswith("data: ")]
        assert events[0]["type"] == "user_message" and events[-1]["type"] == "done"
        full = http.get(f"/api/chats/{chat['id']}", headers=h).json()
        assert [m["role"] for m in full["messages"]] == ["user", "assistant"]
        assert full["title"] == "ADC rate?"
        assert full["messages"][1]["commit_sha"] and "1 kHz" in full["messages"][1]["content"]
        # Other users cannot see this chat.
        assert http.get(f"/api/chats/{chat['id']}", headers={"X-User": "mallory"}).status_code == 404
        assert http.get("/api/chats", headers={"X-User": "mallory"}).json() == []
        # Follow-up question includes the earlier turn as history.
        with http.stream("POST", f"/api/chats/{chat['id']}/messages", json={"content": "And PA0?"}, headers=h) as r:
            list(r.iter_lines())
        first_call_of_followup = [c for c in client.calls if "And PA0?" in str(c["messages"])][0]
        assert first_call_of_followup["messages"][0]["content"] == "ADC rate?"
        assert http.get("/api/status").json()["branch"] == "master"
        assert http.post("/api/admin/calibrate").status_code == 403
        assert http.delete(f"/api/chats/{chat['id']}", headers=h).json() == {"ok": True}


def test_password_and_webhook(settings):
    settings.password = "pw"
    settings.webhook_secret = "s3cret"
    app = create_app(settings, client=FakeClient(), run_sync_loop=False)
    with TestClient(app) as http:
        assert http.get("/api/chats").status_code == 401
        assert http.get("/api/chats", auth=("eli", "pw")).status_code == 200
        assert http.post("/api/webhook", json={"ref": "refs/heads/master"}).status_code == 403
        r = http.post("/api/webhook", json={"ref": "refs/heads/dev"}, headers={"X-Gitlab-Token": "s3cret"})
        assert r.json()["ignored"] == "refs/heads/dev"
        import hashlib
        import hmac

        body = json.dumps({"ref": "refs/heads/master"}).encode()
        sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        r = http.post("/api/webhook", content=body, headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})
        assert r.json() == {"ok": True}
