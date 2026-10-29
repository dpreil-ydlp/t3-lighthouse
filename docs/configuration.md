# Configuration

[Back to the README](../README.md)

The installer writes `~/t3-lighthouse/.env`. Edit it and run `lighthouse up` to apply.

| Variable | Default | Purpose |
| --- | --- | --- |
| `LIGHTHOUSE_BIND_ADDRESS` | `127.0.0.1` | Host address for all published ports |
| `LIGHTHOUSE_API_PORT` | `51821` | HTTP API |
| `LIGHTHOUSE_MCP_PORT` | `51822` | MCP server, path `/mcp/` |
| `LIGHTHOUSE_DASHBOARD_PORT` | `51823` | Dashboard |
| `LIGHTHOUSE_SECRETS_DIR` | `./secrets` | Secret files mounted into the containers |
| `COMPOSE_PROJECT_NAME` | `t3-lighthouse` | Docker Compose project name. An install outside `~/t3-lighthouse` gets its own name, such as `t3-lighthouse-lab-123456789` |
| `LIGHTHOUSE_IMAGE_PREFIX` | `ghcr.io/dpreil-ydlp/t3-lighthouse` | Where to pull the images from; the service name is appended, as in `<prefix>-api` |
| `LIGHTHOUSE_IMAGE_TAG` | `latest` | Image tag to pull; `sha-<commit>` pins a build |
| `LIGHTHOUSE_BUILD` | `0` | `1` builds the images from this checkout instead of pulling |

The installer keeps the default ports when they are free and otherwise takes the next free ones. To choose them yourself, set them for `bash` on the first install:

```sh
curl -fsSL https://raw.githubusercontent.com/dpreil-ydlp/t3-lighthouse/main/install.sh | LIGHTHOUSE_DASHBOARD_PORT=52823 LIGHTHOUSE_API_PORT=52821 LIGHTHOUSE_MCP_PORT=52822 bash
```

The same goes for `LIGHTHOUSE_HOME` (the install folder), `LIGHTHOUSE_REF` (a branch or tag other than `main`), `LIGHTHOUSE_REPO` (a fork to download from, as `owner/repo`; `lighthouse update` keeps using it), and `COMPOSE_PROJECT_NAME`. The installer refuses a project name that another install already uses.

Keep the bind address on localhost or a private network such as Tailscale. The API uses bearer tokens, but it was not built to face the open internet, and the dashboard session cookie is not marked `Secure` because there is no TLS in front of it.

## Settings inside the containers

Set these under `environment:` in `docker-compose.yml`:

| Variable | Default | Purpose |
| --- | --- | --- |
| `WORKER_INTERVAL_SECONDS` | `2` | Seconds between worker passes |
| `LIGHTHOUSE_DERIVE_SUBAGENT_TASKS` | `1` | API: set to `0` to ignore T3 subagent reports |
| `T3_DERIVED_SECOND_KICK_SECONDS` | `1800` | Worker: wait before the second notice for a silent T3 subagent |

## Settings for the Mac scripts

| Variable | Default | Used by |
| --- | --- | --- |
| `LIGHTHOUSE_API_URL` | `http://127.0.0.1:51821` | `briefs.py` |
| `LIGHTHOUSE_CONFIG_DIR` | `~/.config/lighthouse` | `briefs.py` state and work files; `briefs.py` refuses to write outside it |
| `LIGHTHOUSE_AGENT_TOKEN_FILE` | `~/.config/lighthouse/secrets/agent-token` | `briefs.py` |
| `LIGHTHOUSE_CLAUDE_BIN` | `~/.local/bin/claude` | `briefs.py` |
| `LIGHTHOUSE_DERIVE_SUBAGENT_TASKS` | `1` | `collector.py`: set to `0` to stop reading T3 subagents |
| `T3_MCP_URL` | `http://127.0.0.1:3773/mcp` | T3 notices and `t3_connect.py` |
| `T3_MCP_TOKEN_FILE` | `~/.config/lighthouse/secrets/t3-mcp-token` | T3 notices and `t3_connect.py` |
| `LIGHTHOUSE_API_PORT` | `51821` | `install-mac-collector.sh` |
| `LIGHTHOUSE_PYTHON` | first Python 3.11+ on `PATH` | `install-mac-collector.sh`, `lighthouse collector`, `lighthouse t3-connect` |

`collector.py` takes `--host`, `--token-file`, and `--interval` on the command line.

## Alert timing

A registered task stalls when it is `running` and has had no meaningful progress for longer than its `meaningful_progress_seconds` threshold (1,800 seconds if unset). A project's controller policy can change three more values:

| Policy key | Default | Purpose |
| --- | --- | --- |
| `ack_deadline_seconds` | `600` | Time to acknowledge before the incident escalates and a second notification is shown |
| `cooldown_seconds` | `1800` | After an incident, how long the same task cannot open another |
| `progress_deadline_seconds` | the task threshold | After you acknowledge, how long the agent has to report progress |

## The webhook channel

The owner can switch a project's notices to a webhook with `PUT /api/v1/controllers/{controller_id}/channel` and a body of `{"channel": "t3_webhook", "t3_webhook_url": "https://..."}`. The worker then posts each notice as JSON to that HTTPS URL, with an HMAC-SHA256 signature of the body in the `X-T3-Signature` header, keyed with `secrets/t3-webhook-hmac`. The name comes from its first use with T3 Code, but any HTTPS receiver works.
