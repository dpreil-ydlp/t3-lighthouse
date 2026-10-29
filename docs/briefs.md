# Project briefs

[Back to the README](../README.md)

`collector/briefs.py` writes the short brief shown on each project card. It runs on the Mac and sends project text to Anthropic's Claude through your Claude Code CLI. It is off until you schedule it.

Which projects: every active project in Lighthouse whose folder exists on the Mac. You do not pick them one by one. A project is sent on its first run and again whenever it has new session text or commits. To leave a project out, hide or archive it in the dashboard, or run the job by hand with `--project <slug>`.

What each run sends for a project:

- user and assistant message text from Claude Code and Codex sessions that worked in the project folder (tool calls, tool output, and thinking are skipped), newest first;
- the dates and subject lines of up to 40 new Git commits;
- on the first run for a project, up to 8,000 characters each of `README*`, `CLAUDE.md`, `AGENTS.md`, and `memory.md` from the project folder;
- the previous brief.

The total is capped at about 40,000 characters per project, with at most 12 model calls per run.

Before sending, the job removes text that looks like a credential: bearer tokens, common API key formats, private keys, `password=`-style fields, and long hex or base64 strings. This uses regular expressions and is best effort. It will miss secrets in other formats, and it does not remove personal data.

The model is fixed to `claude-haiku-5-5`. The job runs the `claude` CLI with your Claude Code login, so usage counts against your Claude Code account. It drops `ANTHROPIC_API_KEY` and other provider settings from the environment.

To list the projects it would send, without calling Claude, run `python3 collector/briefs.py --dry-run` from `~/t3-lighthouse`. To schedule it every 30 minutes, fill in the placeholders in `collector/scripts/ai.lighthouse.briefs.plist` and load it:

```sh
cd ~/t3-lighthouse
sed -e "s|__PYTHON__|$(command -v python3)|" -e "s|__REPO__|$PWD|" -e "s|__HOME__|$HOME|g" \
  collector/scripts/ai.lighthouse.briefs.plist > ~/Library/LaunchAgents/ai.lighthouse.briefs.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/ai.lighthouse.briefs.plist
```

The Claude Code CLI is only needed for this job.
