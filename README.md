# AskTheCow

A chat server for electrical and hardware engineers. It answers questions about the embedded
firmware repository, so that the engineers do not have to ask the software team.

- It works only on one branch (`master` by default). It does not fetch other branches.
- Each question runs an agent on a checkout of the latest `master` commit.
- A calibration phase reads the full commit history and writes a **repository brief**. The brief
  goes into the agent's prompt, so the agent goes to the correct files quickly.
- When new commits arrive on the remote `master`, the knowledge base grows automatically.
- Chats are saved (SQLite) and listed per user.

## How it works

```
remote master ──fetch──> data/repo.git (bare, master only)
                              │
            ┌─────────────────┴──────────────────┐
            │ calibration / incremental update   │ question
            ▼                                    ▼
  commits in groups ──> Claude: one plain-      data/worktrees/<sha>  (checkout of master head)
  language note per commit + group summary          │
            │                                       ▼
            ▼                                Claude agent + read-only tools:
  SQLite: commit notes (full-text index),     list_dir, find_files, read_file, search_code,
          group summaries                      git_log, git_show, search_history
            │                                       │
            ▼                                       ▼
  Claude agent with tools on master head      answer streamed to the browser, saved in the chat
  writes the repository brief ───────────────> (brief is in the system prompt of every question)
```

### Calibration (first start, or `askthecow calibrate`)

1. Fetch `master`. List all commits that are reachable from `master`, oldest first.
2. Put the commits in groups (size limit `DIGEST_CHUNK_CHARS`). Each group has the commit
   messages, the diffstat and shortened patches. Build output (`.hex`, `.bin`, `.elf`, `.map`)
   and lock files are left out.
3. For each group, Claude writes a note for each commit in hardware terms (pins, peripherals,
   timing, thresholds, old and new values) plus search keywords, and a summary of the group.
   The notes go into a full-text index. The agent searches it with `search_history`.
4. An agent with read-only tools on the `master` head reads the group summaries and the
   current code. It writes the brief: overview, code map, hardware interface index, where values
   live, how to trace common questions, glossary, recent important changes.

Calibration can be stopped and started again. Commits that already have notes are not read again.

### Automatic growth

A background loop fetches `master` every `POLL_INTERVAL_SECONDS` (default 300). A push webhook
can wake it at once. Each question also fetches `master` first, and wakes the loop if the head moved.
For new commits, the loop makes notes and a group summary, and then the agent updates the brief
(it gets the old brief, the new summaries and the changed files). After 25 incremental updates,
the brief is written again from scratch to stop drift.

If somebody rewrites `master` history (force push), the commits that are new get notes, and the
brief is updated against the new head.

### Answering a question

1. Fetch `master` (at most once every `FETCH_MIN_INTERVAL_SECONDS`).
2. Make a checkout of the head commit (shared by concurrent questions on the same commit).
3. Run the agent: system prompt + brief + earlier turns of the chat + the question.
   The tools are read-only and cannot leave the checkout. The agent must confirm facts in the code
   and give `path:line` references.
4. Stream the answer and save it with the commit hash it was produced on.

## Setup

Requirements: Python 3.10 or later, git, an Anthropic API key, and read access to the repository
from the server (SSH key or an HTTPS token).

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
cp .env.example .env    # edit it
set -a && . ./.env && set +a
askthecow calibrate     # optional: run the first calibration in the foreground
askthecow serve         # http://127.0.0.1:8000
```

`serve` starts calibration by itself when there is no brief (`AUTO_CALIBRATE=true`).
While calibration runs, questions still work, but without the brief they are slower.

Other commands:

```bash
askthecow sync                       # fetch and update the knowledge base now
askthecow brief                      # print the current brief
askthecow ask "Which pin is the fan PWM?"
```

With Docker:

```bash
docker build -t askthecow .
docker run -d --env-file .env -p 8000:8000 -v askthecow-data:/data askthecow
```

## Configuration

All settings are environment variables. See [.env.example](.env.example) for the full list.

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | | API key |
| `ASKTHECOW_REPO_URL` | | Repository URL (SSH or HTTPS) |
| `ASKTHECOW_BRANCH` | `master` | The only branch that is used |
| `ASKTHECOW_PROJECT_NAME` | `the firmware` | Name used in prompts |
| `ASKTHECOW_DATA_DIR` | `data` | Clone, checkouts and database |
| `ASKTHECOW_MODEL` | `claude-opus-5-5` | Model for answers |
| `ASKTHECOW_CALIBRATION_MODEL` | `claude-opus-5-5` | Model for commit notes and the brief |
| `ASKTHECOW_ANSWER_EFFORT` | `high` | `low` / `medium` / `high` / `xhigh` / `max` |
| `ASKTHECOW_DIGEST_EFFORT` | `medium` | Effort for commit notes |
| `ASKTHECOW_POLL_INTERVAL_SECONDS` | `300` | How often the remote is checked |
| `ASKTHECOW_PASSWORD` | | When set, the site needs HTTP Basic auth. The user name becomes the chat owner |
| `ASKTHECOW_ADMIN_TOKEN` | | Turns on `POST /api/admin/sync` and `/api/admin/calibrate` (header `X-Admin-Token`) |
| `ASKTHECOW_WEBHOOK_SECRET` | | Turns on `POST /api/webhook` (GitHub signature or GitLab token) |
| `ASKTHECOW_USE_FALLBACKS` | `true` | If the model declines a request, the API retries it on a recommended fallback model |

### Webhook

GitHub: payload URL `https://<server>/api/webhook`, content type `application/json`, secret =
`ASKTHECOW_WEBHOOK_SECRET`, event "push". GitLab: push events, secret token = the same value.
Pushes to other branches are ignored.

## Cost

Calibration sends the full history to the model once. Rough size: the diff text of all commits,
limited to `MAX_DIFF_CHARS_PER_COMMIT` (8000 characters) per commit. For a repository with
thousands of commits this is a large one-time cost. To reduce it, set
`ASKTHECOW_CALIBRATION_MODEL=claude-sonnet-5-5` and/or lower `MAX_DIFF_CHARS_PER_COMMIT`.
After calibration, each push costs one small note request plus one brief update.
Each question is an agent run. The system prompt and brief are cached between requests.

## Security

- The agent tools are read-only. Paths are confined to the checkout, and `.git` is blocked.
  The agent cannot run commands.
- Without `ASKTHECOW_PASSWORD`, the user name comes from the browser and is only a label. Anybody
  who can reach the server can read the code through the bot. Put the server on the internal
  network only, or set the password, or put it behind your SSO reverse proxy.
- The server listens on `127.0.0.1` by default. Set `ASKTHECOW_HOST=0.0.0.0` to expose it.
- Code is sent to the Anthropic API. Make sure your company allows this for the repository.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The tests use a local git repository and a fake model client, so they need no API key.
