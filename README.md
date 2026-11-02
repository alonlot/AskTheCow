# AskTheCow

A chat server for electrical and hardware engineers. It answers questions about the embedded
firmware repository, so that the engineers do not have to ask the software team.

- The agent is **Claude Code in headless mode (`claude -p`)**. Each question starts one
  `claude -p` process in a checkout of the latest `master` commit.
- It works only on one branch (`master` by default). It does not fetch other branches.
- A calibration phase reads the full commit history and writes a **repository brief**. The brief
  goes into the agent's system prompt, so the agent goes to the correct files quickly.
- When new commits arrive on the remote `master`, the knowledge base grows automatically.
- Conversations are saved (SQLite). The web chat lists them per user.
- **One input/output interface for all channels** (`AskService.ask(Inbound) -> Outbound`). The web
  chat and the CLI use it now. You can connect email or other channels later with a small class or
  with an HTTP call (see [Adding a channel](#adding-a-channel-for-example-email)).

## How it works

```
remote master ──fetch──> data/repo.git (bare, master only)
                              │
            ┌─────────────────┴────────────────────┐
            │ calibration / incremental update     │ question (web, CLI, email, ...)
            ▼                                      ▼
  commits in groups ──> claude -p (no tools,     AskService.ask(Inbound)
  JSON schema): a plain-language note per         │  fetch master, checkout data/worktrees/<sha>
  commit + a group summary                         ▼
            │                                    claude -p, read-only, cwd = checkout
            ▼                                    tools: Read, Grep, Glob, read-only git commands
  data/knowledge/commit-notes.txt  ────────────> (it greps the knowledge files too)
  data/knowledge/history.md                        │
            │                                      ▼
            ▼                                    Outbound: answer text, commit sha, tool calls
  claude -p agent on the master checkout         saved in the conversation, streamed to the web UI
  writes data/knowledge/brief.md ──────────────> brief goes into the system prompt of every answer
```

### Calibration (first start, or `askthecow calibrate`)

1. Fetch `master`. List all commits that are reachable from `master`, oldest first.
2. Put the commits in groups (size limit `DIGEST_CHUNK_CHARS`). Each group has the commit messages,
   the diffstat and shortened patches. Build output (`.hex`, `.bin`, `.elf`, `.map`) and lock files
   are left out.
3. For each group, `claude -p` (no tools, `--json-schema`) writes a note for each commit in hardware
   terms (pins, peripherals, timing, thresholds, old and new values) plus search keywords, and a
   summary of the group.
4. A `claude -p` agent with read-only tools on the `master` head reads the group summaries and the
   current code, and writes the brief: overview, code map, hardware interface index, where values
   live, how to trace common questions, glossary, recent important changes.

The knowledge is stored in SQLite and also written as plain files in `data/knowledge/`
(`brief.md`, `history.md`, `commit-notes.txt`). The answer agent greps these files for
"when / why did this change" questions.

Calibration can be stopped and started again. Commits that already have notes are not read again.

### Automatic growth

A background loop fetches `master` every `POLL_INTERVAL_SECONDS` (default 300). A push webhook can
wake it at once. Each question also fetches `master` first, and wakes the loop if the head moved.
For new commits, the loop makes notes and a group summary, and then the agent updates the brief.
After 25 incremental updates (`FULL_REBRIEF_EVERY`), the brief is written again from scratch.

### Answering a question

1. Fetch `master` (at most once every `FETCH_MIN_INTERVAL_SECONDS`).
2. Make a checkout of the head commit (shared by concurrent questions on the same commit).
3. Run `claude -p` in the checkout. The system prompt has the answer rules and the brief.
   The prompt has the earlier messages of the conversation and the question.
4. Stream the answer and save it with the commit hash it was produced on.

At most `MAX_CONCURRENT_AGENTS` (default 3) agents run at the same time. Other questions wait.

### The `claude -p` command

Answers and the brief use this tool setup (see `askthecow/claude_cli.py`):

```
claude -p --output-format stream-json --verbose --model <model> --effort <effort>
  --permission-mode dontAsk --restricted --strict-mcp-config --no-session-persistence
  --tools Read,Grep,Glob,Bash
  --allowedTools Read Grep Glob "Bash(git log:*)" "Bash(git show:*)" "Bash(git blame:*)" "Bash(git diff:*)" ...
  --disallowedTools "Bash(git * --output*)" "Bash(git * --ext-diff*)" "Bash(git * --no-index*)" Edit Write ...
  --append-system-prompt-file <rules + brief> --add-dir data/knowledge
```

- `dontAsk`: a tool call that is not allowed is denied at once. Nothing waits for a person.
- `--restricted`: settings files (also any `.claude/` settings in the firmware repo) are ignored, and
  file tools stay inside the checkout and `data/knowledge`.
- Tested: commands that read inside the checkout run. Reads outside it, file writes and network
  commands are denied.

## Setup

Requirements: Python 3.10 or later, git, Claude Code (`claude`), and read access to the repository
from the server (SSH key or an HTTPS token).

```bash
# Claude Code CLI
curl -fsSL https://claude.ai/install.sh | bash
# Authentication for claude -p: ANTHROPIC_API_KEY, or a token from `claude setup-token`
export ANTHROPIC_API_KEY=...

python3 -m venv .venv && . .venv/bin/activate
pip install -e .
cp .env.example .env    # edit it
set -a && . ./.env && set +a
askthecow calibrate     # optional: run the first calibration in the foreground
askthecow serve         # http://127.0.0.1:8000
```

`serve` starts calibration by itself when there is no brief (`AUTO_CALIBRATE=true`). While
calibration runs, questions still work, but without the brief they are slower.

Other commands:

```bash
askthecow sync                                   # fetch and update the knowledge base now
askthecow brief                                  # print the current brief
askthecow ask "Which pin is the fan PWM?"        # one question from the terminal
askthecow ask "And its frequency?" --thread fan  # continue a CLI conversation
askthecow ask "..." --json                       # answer as JSON
```

With Docker:

```bash
docker build -t askthecow .
docker run -d --env-file .env -p 8000:8000 -v askthecow-data:/data askthecow
```

## Adding a channel (for example email)

All channels use the same interface in `askthecow/service.py`:

```python
from askthecow.service import Inbound

answer = await service.ask(Inbound(
    text=mail_body,                    # the question
    channel="email",                   # any name
    user="eli@company.com",            # who asked
    thread_id=mail_thread_id,          # same thread_id = same conversation (history is kept)
    title=mail_subject,                # title of a new conversation
    instructions="Write the answer as a plain-text email.",   # optional, per channel
    meta={"message_id": mail_message_id},                     # optional, saved with the question
))
answer.text          # the answer
answer.commit_sha    # the master commit it is based on
answer.error         # True if the agent failed (answer.text has the reason)
```

There are two ways to connect a channel:

**1. A channel class that runs inside the server.** Write a subclass of `askthecow.channels.Channel`
with an `async run()` loop (receive mails, call `self.service.ask(...)`, send replies). Then start
the server with `ASKTHECOW_CHANNELS=mycompany.mail_channel:MailChannel`. The server starts it and
restarts it if it fails. `askthecow/channels.py` has a full example.

**2. An HTTP call from another program** (for example a mail flow in Power Automate, n8n or a
script). Set `ASKTHECOW_API_TOKEN`, then:

```bash
curl -X POST http://server:8000/api/ask -H "X-Api-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"text": "What is the over-current limit?", "channel": "email", "user": "eli@company.com",
       "thread_id": "<CAF123@mail.company.com>", "title": "Over-current limit",
       "instructions": "Write the answer as a plain-text email."}'
# -> {"text": "...", "conversation_id": "...", "message_id": 12, "commit_sha": "...", "error": false}
```

Conversations from other channels are saved in the same database, but the web chat shows only web
conversations.

## Configuration

All settings are environment variables `ASKTHECOW_<NAME>`. See [.env.example](.env.example) for
the full list.

| Variable | Default | Purpose |
|---|---|---|
| `ASKTHECOW_REPO_URL` | | Repository URL (SSH or HTTPS) |
| `ASKTHECOW_BRANCH` | `master` | The only branch that is used |
| `ASKTHECOW_PROJECT_NAME` | `the firmware` | Name used in prompts |
| `ASKTHECOW_DATA_DIR` | `data` | Clone, checkouts, database and knowledge files |
| `ASKTHECOW_CLAUDE_BIN` | `claude` | Path of the Claude Code CLI |
| `ASKTHECOW_MODEL` | `claude-opus-5-5` | Model for answers (`--model`; aliases such as `sonnet` also work) |
| `ASKTHECOW_CALIBRATION_MODEL` | `claude-opus-5-5` | Model for commit notes and the brief |
| `ASKTHECOW_ANSWER_EFFORT` | `high` | `--effort`: `low` / `medium` / `high` / `xhigh` / `max` |
| `ASKTHECOW_ANSWER_MAX_BUDGET_USD` | `0` (no limit) | `--max-budget-usd` for each answer |
| `ASKTHECOW_ANSWER_TIMEOUT_SECONDS` | `900` | The answer process is stopped after this time |
| `ASKTHECOW_MAX_CONCURRENT_AGENTS` | `3` | Answer agents that run at the same time |
| `ASKTHECOW_POLL_INTERVAL_SECONDS` | `300` | How often the remote is checked |
| `ASKTHECOW_CHANNELS` | | Channel classes to start, `module:Class,module:Class` |
| `ASKTHECOW_PASSWORD` | | When set, the site needs HTTP Basic auth. The user name becomes the chat owner |
| `ASKTHECOW_API_TOKEN` | | Turns on `POST /api/ask` (header `X-Api-Token`) |
| `ASKTHECOW_ADMIN_TOKEN` | | Turns on `POST /api/admin/sync` and `/api/admin/calibrate` (header `X-Admin-Token`) |
| `ASKTHECOW_WEBHOOK_SECRET` | | Turns on `POST /api/webhook` (GitHub signature or GitLab token) |

### Webhook

GitHub: payload URL `https://<server>/api/webhook`, content type `application/json`, secret =
`ASKTHECOW_WEBHOOK_SECRET`, event "push". GitLab: push events, secret token = the same value.
Pushes to other branches are ignored.

## Cost

Calibration sends the full history to the model once: the diff text of all commits, limited to
`MAX_DIFF_CHARS_PER_COMMIT` (8000 characters) per commit. For a repository with thousands of
commits this is a large one-time cost. To reduce it, set `ASKTHECOW_CALIBRATION_MODEL=sonnet`
and/or lower `MAX_DIFF_CHARS_PER_COMMIT`. After calibration, each push costs one small note request
plus one brief update. Each answer is one agent run; `ASKTHECOW_ANSWER_MAX_BUDGET_USD` limits it.
Each saved answer has its cost in `meta.cost_usd`.

## Security

- The agent can only read. See [The `claude -p` command](#the-claude--p-command).
- Without `ASKTHECOW_PASSWORD`, the user name comes from the browser and is only a label. Anybody
  who can reach the server can read the code through the bot. Put the server on the internal
  network only, or set the password, or put it behind your SSO reverse proxy.
- The server listens on `127.0.0.1` by default. Set `ASKTHECOW_HOST=0.0.0.0` to expose it.
- Code is sent to Anthropic by Claude Code. Make sure your company allows this for the repository.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The tests use a local git repository and a fake `claude` executable (`tests/fake_claude.py`) that
writes the same `stream-json` output, so they need no API key and no network.
