---
name: claude-code
description: Collaborate with native Claude Code on your local subscription. Request a review, steer the same conversation into implementation in a shared checkout, or start a strictly read-only job. Also find, resume, cancel, or check quota.
compatibility: Local signed-in Claude Code, uv, and a POSIX Prime Agent host. No API key needed.
---

# Claude Code

Prime drives; Claude Code works in the same checkout. Use only in trusted repositories. Ask for inspection on a review turn, then use `send` to request edits or tests. Changes are visible immediately; coordinate file ownership and inspect the diff. There is no separate worktree, patch application, or rollback.

Shared jobs use Claude Code's built-in tools and native permission rules from user, project, local, and managed settings. The skill adds no grants. Operations that need interactive approval are denied, not relayed to Prime. Use `read_only=True` at start to restrict tools to Read, Glob, and Grep for the entire conversation. Older jobs stay read-only on resume. Both profiles disable customizations and MCP; neither is an OS sandbox. Don't put secrets in the brief.

```python
job = claude_code.start("Review the patch without editing; cite file:line", cwd=repo, name="patch-review", shell=bash)
jid = job["id"]
claude_code.send(jid, "Implement your suggested fix and run the focused tests", shell=bash)
claude_code.redirect(jid, "Stop the current pass; focus on cleanup", shell=bash)
```

Pass `shell=bash` to make Prime supervise the job and send a completion notification. If omitted, run the returned `command` using Prime `bash` when non-null. Do not use persistent shell jobs. A worker burst (including queued turns) is capped at 15 minutes. A `queued` receipt is not proof of Claude seeing the message. Queued sends run in order at turn boundaries; a redirect supersedes older queued messages and asks the SDK to interrupt the current turn. It does not undo tool effects. `cancel(jid)` requests cancellation; use the original shell handle to stop a stuck controller. Don't resend or retry ambiguous submissions automatically.

On builds with Claude completion notifications, `shell=bash` reports `ready`, `blocked`, or `failed`. A result excerpt is included only when Agents-view previews are enabled for that job. Stock handles and manually launched commands keep the ordinary bash notification.

After a Claude notification or `[bash-done]`, read `claude_code.show(jid)` for the handoff, exact Claude session, model/version, cache observations, and native transcript path. `blocked` means Claude finished its turn with a decision request or permission denial. Use `send` to clarify or change the task; a message cannot grant tool permission. While working, `show` can report last observed activity. Discover older jobs locally with `claude_code.history(repo=repo, query="cleanup")`; `send` on an idle job resumes the recorded session in its original cwd. History and quota reads make no Claude requests. A missing worktree or deleted Claude transcript is not automatically substituted.

On the experimental Prime build, Claude jobs in saved sessions appear in Agents view when `claude-agents-view.enabled` exists in the Prime agent directory. This writes a private, bounded preview of the exchange. Pass `agents_view=False` to skip it for one job, or remove the preference file to disable it by default. `agents_view=True` opts in explicitly. The row is read-only; use the skill to steer. Native Claude history remains authoritative. Stock Prime does not display these previews.

Idle age is time since the last result, not a prompt-cache expiry timer. Prior token counters report cache use on that turn; they cannot tell whether the next turn will hit. `claude_code.usage()` reports the latest observed 5-hour/weekly windows. Missing or stale estimates do not gate work. Quota failures are not retried. Existing Claude-plan extra-usage settings are outside the skill; keep extra usage off if you require no paid overage.

This worker checks for native first-party Claude login and removes ambient API credentials/alternate providers instead of falling back to them. It uses your installed Claude binary. To verify the local package: `uv run --locked --project <skill-dir> python -m unittest discover -s <skill-dir>/tests`. In an existing Prime session use `/reload` or open another session to load the skill.
