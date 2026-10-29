# T3 Code

[Back to the README](../README.md)

## What Lighthouse reads from T3

If `~/.t3/userdata` exists, the collector opens T3's SQLite database read-only, in place, and reads project paths and names, thread and subagent IDs, titles, status, and timestamps. It never copies or writes the database.

T3 does not publish that database as an API. The schema is internal to T3 and can change in any T3 update. When that happens, the T3 part of the collector can stop working until Lighthouse is updated; the rest keeps running.

## How Lighthouse connects to T3

Stall notices for subagents go into the parent thread through T3's MCP server, which takes OAuth tokens. T3 advertises this at `http://127.0.0.1:3773/.well-known/oauth-authorization-server`: client registration, the authorization code flow with PKCE, and no refresh tokens.

`lighthouse t3-connect` (run by the installer when it finds T3) does the following:

1. Asks T3's own command for a pairing code that expires in five minutes and grants only `orchestration:read` and `orchestration:operate`: `~/.t3/bin/t3 auth pairing create --ttl 5m --scope orchestration:read --scope orchestration:operate`.
2. Registers Lighthouse with T3 and approves it with that code at the "approval required" access level. This is the same approval T3's "Connect agent" page asks for. A turn that a Lighthouse notice starts cannot run with more than that.
3. Saves the token to `~/.config/lighthouse/secrets/t3-mcp-token` (mode 0600). The pairing code is used once and never saved.

T3 lists the token under its sessions as `Lighthouse`; the T3 versions we tested issued it for 30 days. The collector renews it three days before it expires, with a new pairing code, and revokes the old one. The Incidents page shows when it expires.

Without the `t3` command, create a pairing code in T3 under Settings, Sharing & access, and pass it on stdin:

```sh
pbpaste | ~/t3-lighthouse/bin/lighthouse t3-connect --pairing-code-stdin
```

Then the collector cannot renew the token; run the command again before it expires.

To stop notices into T3 and revoke the token: `lighthouse t3-connect --disconnect`.

## What a notice looks like

When T3 shows a subagent as running but it has finished no command, tool call, or message for 30 minutes, Lighthouse opens an incident and posts into the parent thread:

> Lighthouse: your subagent “Write the migration” has been marked working but silent for 31 min. Check on it: read its thread, nudge it, or cancel it. t3code://threads/...

If the subagent is still silent 30 minutes later, a second notice says it needs a decision. There is no third. Before sending, the collector checks the subagent is still running; a subagent that finished or is waiting for your approval gets no notice. These incidents do not show macOS notifications.

`LIGHTHOUSE_DERIVE_SUBAGENT_TASKS=0` turns this off ([configuration](configuration.md)).
