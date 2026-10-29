# T3 Code

[Back to the README](../README.md)

## What Lighthouse reads from T3

If `~/.t3/userdata` exists, the collector opens T3's SQLite database read-only, in place, and reads project paths and names, thread and subagent IDs, titles, status, and timestamps. It never copies or writes the database.

T3 does not publish that database as an API. The schema is internal to T3 and can change in any T3 update. When that happens, the T3 part of the collector can stop working until Lighthouse is updated; the rest keeps running.

## How Lighthouse connects to T3

Stall notices for subagents go into the parent thread through T3's MCP server, which takes OAuth tokens. T3 advertises this at `http://127.0.0.1:3773/.well-known/oauth-authorization-server`: client registration, the authorization code flow with PKCE, and no refresh tokens.

`lighthouse t3-connect` (run by the installer when it finds T3) does the following:

1. Asks T3's own command for a pairing code that expires in five minutes and grants only `orchestration:read` and `orchestration:operate`: `~/.t3/bin/t3 auth pairing create --ttl 5m --scope orchestration:read --scope orchestration:operate`.
2. Registers Lighthouse with T3 and approves it with that code at the "approval required" access level. A turn that a Lighthouse notice starts cannot run with more than that.
3. Saves the token to `~/.config/lighthouse/secrets/t3-mcp-token` (mode 0600), with the ID of the T3 session it belongs to. The pairing code is used once and never saved.

T3 lists the token under its sessions as `Lighthouse`; the T3 versions we tested issued it for 30 days. The collector renews it three days before it expires, with a new pairing code, and then revokes the session the old token used. The Incidents page shows when it expires.

### Which T3 call approves Lighthouse

Step 2 posts the pairing code to `/oauth/mcp/decision`, the call T3's own Connect agent page makes when you type a pairing code there. T3 does not document this call. We read it in the T3 Code app itself: `T3 Code (Beta).app/Contents/Resources/app.asar`, file `apps/server/dist/binCli-BH36ZN71.mjs` in T3 Code 0.1.4, where the server's `mcpOAuth` routes define `GET /oauth/mcp/authorize` (it sends the browser to `/connect-agent`) and `POST /oauth/mcp/decision` with a `pairing-code` decision. The well-known metadata above names only the authorize, token, and registration endpoints.

Because a T3 update could change that call, the installer does not stop when the approval fails. It prints one step instead:

```sh
lighthouse t3-connect --browser
```

This opens T3's Connect agent page for Lighthouse. Approve it there with a pairing code from T3 Code: Settings, Sharing & access. T3 then sends the browser back to `http://127.0.0.1:51829/t3-callback`, where `t3-connect` waits for up to five minutes, and the token is saved as above.

Without the `t3` command, the collector cannot renew the token; run `lighthouse t3-connect --browser` again before it expires. You can also pass a pairing code on stdin: `pbpaste | lighthouse t3-connect --pairing-code-stdin`.

### Disconnecting and revoking

`lighthouse t3-connect --disconnect` stops notices into T3: it revokes the T3 session behind this install's token with `t3 auth session revoke` and deletes the token file. Uninstall does the same. Only that one session is revoked; another install's session, even one labelled `Lighthouse`, keeps working.

If T3 does not confirm the revocation (the `t3` command is missing, or it fails), Lighthouse says so and prints the step that finishes it: `t3 auth session revoke <session ID>`, or in T3 Code, Settings, Sharing & access, revoke the Lighthouse session. The session ID stays in `~/.config/lighthouse/secrets/t3-mcp-revoke-pending`, and the next `t3-connect`, `--disconnect`, or collector renewal check tries it again.

## What a notice looks like

When T3 shows a subagent as running but it has finished no command, tool call, or message for 30 minutes, Lighthouse opens an incident and posts into the parent thread:

> Lighthouse: your subagent “Write the migration” has been marked working but silent for 31 min. Check on it: read its thread, nudge it, or cancel it. t3code://threads/...

Lighthouse sends it with T3's `t3_thread_send` tool in `auto` mode. If the parent agent is idle, the notice starts a new turn of it in that thread; if it is working, T3 adds the notice to the running turn. The turn uses the thread's provider and account, like any message you send there, at no more than "approval required" access.

If the subagent is still silent 30 minutes later, a second notice says it needs a decision. There is no third. Before sending, the collector checks the subagent is still running; a subagent that finished or is waiting for your approval gets no notice. These incidents do not show macOS notifications.

To stop the notices but keep the incidents on the dashboard, run `lighthouse t3-connect --disconnect`. `LIGHTHOUSE_DERIVE_SUBAGENT_TASKS=0` turns subagent stall alerts off altogether ([configuration](configuration.md)).

Alerts for tasks that agents register over MCP never go into T3. They are macOS notifications only.
