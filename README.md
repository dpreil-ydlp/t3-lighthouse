# T3 Lighthouse

Lighthouse watches your coding-agent sessions and tells you when a project stops moving.

A collector on your Mac reads local Claude Code and Codex session files and notes when the ChatGPT, Codex, and Claude desktop apps write to their data folders. It sends events to a small server. The server groups those events by project, tracks tasks and parent threads, and opens an incident when work goes quiet past a deadline you set. A dashboard shows each project's state, open incidents, and a short written brief. An MCP server lets agents register tasks and report progress.

Lighthouse observes and sends notices. It never starts or stops an agent.

## Parts

| Path | What it is |
| --- | --- |
| `lighthouse/` | FastAPI HTTP API, MCP server, stall worker, and admin CLI |
| `db/migrations/` | PostgreSQL schema |
| `dashboard/` | Next.js dashboard |
| `collector/collector.py` | Mac collector for session files and app activity |
| `collector/briefs.py` | Optional job that writes project briefs with the Claude Code CLI |
| `collector/t3_read.py`, `collector/t3_mcp.py` | Optional T3 Code integration |
| `docker-compose.yml` | PostgreSQL, API, MCP server, worker, and dashboard |

## Requirements

- Docker with Docker Compose v2 on the server. The server can be the same Mac.
- macOS with Python 3.11 or later for the collector. The `python3` that ships with macOS is older; install a newer one, for example with Homebrew, and put it first on `PATH`.
- The Claude Code CLI, only if you want project briefs.
- Node.js 26 or later, only to build or test the dashboard outside Docker. The dashboard image already includes Node.js 26.
- T3 Code is optional. Everything else works without it.

## Install

1. Create the configuration and secrets.

   ```sh
   cp .env.example .env
   mkdir -m 700 secrets
   openssl rand -hex 32 > secrets/db-password
   openssl rand -hex 32 > secrets/dashboard-session-key
   openssl rand -hex 32 > secrets/t3-webhook-hmac
   chmod 600 secrets/*
   ```

   `t3-webhook-hmac` signs notices for the optional webhook channel described under [Notices](#notices). Compose mounts it into the worker, so create it even if you never use that channel.

2. Start the database and API, then apply the migrations that the database image does not run on first start (004 onward).

   ```sh
   docker compose up -d db api
   for f in db/migrations/0*.sql; do
     case "$f" in *00[1-3]_*) continue;; esac
     docker compose exec -T db psql -v ON_ERROR_STOP=1 -U lighthouse_root -d lighthouse < "$f"
   done
   ```

3. Create the owner login and the agent credential. Each command writes a new token file into the secrets directory and does not print the token.

   ```sh
   docker compose exec -T api admin-entrypoint bootstrap --name owner --token-file /host-secrets/owner-token
   docker compose exec -T api admin-entrypoint issue-principal --name agent --kind agent --token-file /host-secrets/agent-token
   ```

   On Linux with rootful Docker, the API container writes these files as root. Read them with `sudo cat`, or `sudo chown` them to your user, in the steps below.

4. Start the rest.

   ```sh
   docker compose up -d
   curl -fsS http://127.0.0.1:51821/healthz
   ```

   The commands here use the default ports. If you changed them in `.env`, use your values instead.

   Open the dashboard at `http://127.0.0.1:51823` and log in with the contents of `secrets/owner-token`.

5. Install the collector on the Mac. Put the agent token at `~/.config/lighthouse/secrets/agent-token`, then run the installer:

   ```sh
   mkdir -p -m 700 ~/.config/lighthouse/secrets
   cp secrets/agent-token ~/.config/lighthouse/secrets/agent-token
   chmod 600 ~/.config/lighthouse/secrets/agent-token
   scripts/install-mac-collector.sh --host 127.0.0.1 --port 51821
   ```

   This writes and loads a LaunchAgent named `ai.lighthouse.collector`. If the server is another machine, copy `secrets/agent-token` from the server instead, for example with `scp`, and use the server's address for `--host`. See [What the collector sends](#what-the-collector-sends).

6. Optional: schedule project briefs. Read [Project briefs](#project-briefs) first: this job sends project text to Claude.

## What the collector sends

The collector sends metadata to your Lighthouse server and nowhere else. It never sends message text from your sessions.

- **Claude Code and Codex sessions** (`~/.claude/projects`, `~/.codex/sessions`): an event for each new session record with the working directory, the session ID, the record type (for example `user` or `assistant`), and the record timestamp. The message body is not read into the event.
- **Thread titles**: the session title each tool stores. For Claude Code this is the `summary` record that Claude Code writes into the session file; for Codex it is the name in `~/.codex/session_index.jsonl`.
- **Project folders**: the paths and folder names of the direct subfolders of `~/Projects` (or `~/projects`) that a session has worked in.
- **Desktop apps** (ChatGPT, Codex, and Claude): when a file in the app's data folder changes, a SHA-256 hash of the file's relative path and its modification time. File names and contents are not sent.
- **T3 Code**, if installed: project paths and names, thread and subagent IDs, titles, status, and timestamps from T3's database. See [T3 Code integration](#t3-code-integration).

It keeps its read positions in `~/.config/lighthouse/collector-state.json` and its log in `~/.config/lighthouse/collector.log`.

## Notices

When a task misses its deadline, the worker opens an incident. By default the Mac collector then shows a macOS notification, through `terminal-notifier` if it is installed and `osascript` otherwise. If nobody acknowledges the incident before the deadline (10 minutes by default), it escalates and the collector shows a second notification.

The owner can switch a project's notices to a webhook with `PUT /api/v1/controllers/{controller_id}/channel` and a body of `{"channel": "t3_webhook", "t3_webhook_url": "https://..."}`. The worker then posts each notice as JSON to that HTTPS URL, with an HMAC-SHA256 signature of the body in the `X-T3-Signature` header, keyed with `secrets/t3-webhook-hmac`. The name comes from its first use with T3 Code, but any HTTPS receiver works.

## Connect an agent

Point the agent's MCP client at `http://127.0.0.1:51822/mcp/` and send the agent token as `Authorization: Bearer <token>`. Keep the trailing slash: `/mcp` answers with a redirect, and some clients do not follow it. Use the token from `secrets/agent-token` or issue one per agent with `issue-principal` as in step 3.

The tools let an agent list projects, read a project's brief, create tasks, report progress, register itself as a project's controller, register parent threads, and acknowledge incidents. `review_task` is for the owner only: it needs the owner token, and the API rejects agent tokens.

The same token works with the HTTP API on port 51821.

To check the MCP server from the command line, `lighthouse/mcp_probe.py` lists projects or calls one tool:

```sh
docker compose exec -T mcp python -m lighthouse.mcp_probe \
  --endpoint http://127.0.0.1:8000/mcp/ --token-file /run/secrets/agent-token list_projects
```

### Example: make a stall alert fire

Stall monitoring works per task. A task stalls when it is `running` and has had no meaningful progress for longer than its `meaningful_progress_seconds` threshold (30 minutes if unset). This example sets a 60-second threshold with the HTTP API. The MCP tools `create_task` and `report_progress` take the same fields.

```sh
TOKEN=$(cat secrets/agent-token)
API=http://127.0.0.1:51821
post() {
  curl -fsS -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -H "Idempotency-Key: $1" -d "$3" "$API$2"
}
id() { python3 -c "import json,sys; print(json.load(sys.stdin)['$1']['id'])"; }

PROJECT=$(post demo-project-1 /api/v1/projects '{"name": "Stall demo"}' | id project)
TASK=$(post demo-task-1 /api/v1/projects/$PROJECT/tasks \
  '{"title": "Demo task", "thresholds": {"meaningful_progress_seconds": 60}}' | id task)
post demo-progress-1 /api/v1/events "{\"task_id\": \"$TASK\", \"status\": \"running\",
  \"meaningful\": true, \"summary\": \"Started the demo task\",
  \"source_event_id\": \"demo-progress-1\", \"observed_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
```

Every write needs an `Idempotency-Key` header. A repeated key with the same body returns the first response, so change the keys (and `source_event_id`) to run the example again.

Wait about 70 seconds, then list incidents:

```sh
curl -fsS -H "Authorization: Bearer $TOKEN" $API/api/v1/incidents | python3 -m json.tool
```

You should see an incident with `"kind": "stall"`, `"state": "open"`, `"task_title": "Demo task"`, and `"reason": "No meaningful progress within the task threshold"`. It also appears on the dashboard's Incidents page. Another meaningful progress event for the task resolves it.

## Project briefs

`collector/briefs.py` writes the short brief shown on each project card. It runs on the Mac and sends project text to Anthropic's Claude through your Claude Code CLI. Read this section before you turn it on.

Which projects: every active project in Lighthouse whose folder exists on the Mac. You do not pick them one by one. A project is sent on its first run and again whenever it has new session text or commits. To leave a project out, hide or archive it in the dashboard, or run the job by hand with `--project <slug>`.

What each run sends for a project:

- user and assistant message text from Claude Code and Codex sessions that worked in the project folder (tool calls, tool output, and thinking are skipped), newest first;
- the dates and subject lines of up to 40 new Git commits;
- on the first run for a project, up to 8,000 characters each of `README*`, `CLAUDE.md`, `AGENTS.md`, and `memory.md` from the project folder;
- the previous brief.

The total is capped at about 40,000 characters per project, with at most 12 model calls per run.

Before sending, the job removes text that looks like a credential: bearer tokens, common API key formats, private keys, `password=`-style fields, and long hex or base64 strings. This uses regular expressions and is best effort. It will miss secrets in other formats, and it does not remove personal data.

The model is fixed to `claude-haiku-5-5`. The job runs the `claude` CLI with your Claude Code login, so usage counts against your Claude Code account. It drops `ANTHROPIC_API_KEY` and other provider settings from the environment.

To list the projects it would send, without calling Claude, run `python3 collector/briefs.py --dry-run`. To schedule it every 30 minutes, fill in the placeholders in `collector/scripts/ai.lighthouse.briefs.plist` and load it:

```sh
sed -e "s|__PYTHON__|$(command -v python3)|" -e "s|__REPO__|$PWD|" -e "s|__HOME__|$HOME|g" \
  collector/scripts/ai.lighthouse.briefs.plist > ~/Library/LaunchAgents/ai.lighthouse.briefs.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/ai.lighthouse.briefs.plist
```

## Configuration

Docker Compose reads these from `.env`:

| Variable | Default | Purpose |
| --- | --- | --- |
| `LIGHTHOUSE_BIND_ADDRESS` | `127.0.0.1` | Host address for all published ports |
| `LIGHTHOUSE_API_PORT` | `51821` | HTTP API |
| `LIGHTHOUSE_MCP_PORT` | `51822` | MCP server, path `/mcp/` |
| `LIGHTHOUSE_DASHBOARD_PORT` | `51823` | Dashboard |
| `LIGHTHOUSE_SECRETS_DIR` | `./secrets` | Secret files mounted into the containers |

These are read inside the containers. Set them under `environment:` in `docker-compose.yml` to change them:

| Variable | Default | Purpose |
| --- | --- | --- |
| `WORKER_INTERVAL_SECONDS` | `2` | Seconds between worker passes |
| `LIGHTHOUSE_DERIVE_SUBAGENT_TASKS` | `1` | API: set to `0` to ignore T3 subagent reports |
| `T3_DERIVED_SECOND_KICK_SECONDS` | `1800` | Worker: wait before the second notice for a silent T3 subagent |

The Mac scripts read these from the environment:

| Variable | Default | Used by |
| --- | --- | --- |
| `LIGHTHOUSE_API_URL` | `http://127.0.0.1:51821` | `briefs.py` |
| `LIGHTHOUSE_CONFIG_DIR` | `~/.config/lighthouse` | `briefs.py` state and work files; `briefs.py` refuses to write outside it |
| `LIGHTHOUSE_AGENT_TOKEN_FILE` | `~/.config/lighthouse/secrets/agent-token` | `briefs.py` |
| `LIGHTHOUSE_CLAUDE_BIN` | `~/.local/bin/claude` | `briefs.py` |
| `LIGHTHOUSE_DERIVE_SUBAGENT_TASKS` | `1` | `collector.py`: set to `0` to stop reading T3 subagents |
| `T3_MCP_URL` | `http://127.0.0.1:3773/mcp` | T3 notices |
| `T3_MCP_TOKEN_FILE` | `~/.config/lighthouse/secrets/t3-mcp-token` | T3 notices |
| `LIGHTHOUSE_API_PORT` | `51821` | `install-mac-collector.sh` |

`collector.py` takes `--host`, `--token-file`, and `--interval` on the command line.

Keep the bind address on localhost or a private network such as Tailscale. The API uses bearer tokens, but it was not built to face the open internet. The dashboard session cookie is not marked `Secure` because there is no TLS in front of it.

## T3 Code integration

This part is optional. If T3 Code is installed, the collector reads T3's local SQLite database to link sessions to T3 threads and subagents. Lighthouse can also post stall notices into a T3 thread through T3's MCP server.

T3 does not publish that database as an API. Lighthouse opens it read-only and never writes to it, but the schema is internal to T3 and can change in any T3 update. When that happens, the T3 part of the collector can stop working until Lighthouse is updated. The rest of Lighthouse keeps running.

## Tests

The API tests need PostgreSQL at host `db` and create and drop their own `lighthouse_test` database. They refuse to run against an existing one. Run them inside the API container:

```sh
docker compose exec -T api run-tests
```

Collector tests run without a database. They need pytest (`python3 -m pip install pytest`):

```sh
python3 -m pytest collector/tests
```

Dashboard (Node.js 26 or later):

```sh
cd dashboard && npm ci && npm run build && node tests/test_brief_render.cjs && node tests/test_session_key.cjs
```

## Security

To report a vulnerability, see [SECURITY.md](SECURITY.md).

## License

MIT. See [LICENSE](LICENSE).
