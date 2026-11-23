"""One Prime-supervised burst of work against the installed Claude Code."""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient,
                              RateLimitEvent, ResultMessage, SystemMessage, ToolUseBlock)
from . import TOOLS, change, job_dir, lock, read, root, write

SAFE_ENV = {"HOME", "USER", "LOGNAME", "PATH", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE",
            "SHELL", "TERM", "__CF_USER_TEXT_ENCODING"}
GUIDANCE = ("If a tool is denied, do not work around it. "
            "If you need permission or a decision, finish the current turn with BLOCKED: and one specific question; "
            "the supervisor can answer in a follow-up. Never use tools outside the requested workspace.")


def installed_claude(cwd, config_dir=None, setting_sources=()):
    path = shutil.which("claude")
    if not path:
        raise RuntimeError("Install Claude Code and sign in first")
    # SDK inherits the controller environment. Keep login via native Claude only.
    env = {k: v for k, v in os.environ.items() if k in SAFE_ENV}
    # An explicitly set default directory changes the native login namespace on macOS.
    if config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    auth = subprocess.run([path, "--safe-mode", "--setting-sources=" + ",".join(setting_sources),
                           "auth", "status"], env=env, cwd=cwd,
                          capture_output=True, text=True, timeout=15)
    if auth.returncode:
        raise RuntimeError("Claude subscription login unavailable; run claude auth login")
    status = json.loads(auth.stdout)
    if (not status.get("loggedIn") or status.get("authMethod") != "claude.ai"
            or status.get("apiProvider") != "firstParty"):
        raise RuntimeError("Expected first-party claude.ai login; no API auth fallback")
    return path, env


def cache_usage(event):
    info = event.rate_limit_info.raw
    unified = info.get("unifiedWindows") or {}
    now = time.time()
    with lock(root() / "usage.lock"):
        path = root() / "usage.json"
        last = read(path) if path.exists() else {"windows": {}}
        fields = {kind: raw for kind, raw in unified.items() if kind in {"five_hour", "seven_day"}}
        if info.get("rateLimitType") in {"five_hour", "seven_day"}:
            fields.setdefault(info["rateLimitType"], info)
        for kind, raw in fields.items():
            last["windows"][kind] = dict(used_fraction=raw.get("utilization"), resets_at=raw.get("resetsAt"),
                                         limit_status=raw.get("status"), observed_at=now)
        last.update(observed_at=now, limit_status=info.get("status"), resets_at=info.get("resetsAt"),
                    using_overage=info.get("isUsingOverage"))
        write(path, last)
    return info


def queued(data):
    items = [m for m in data["mailbox"] if m["status"] == "queued"]
    return next((m for m in reversed(items) if m["mode"] != "send"), items[0] if items else None)


def select(data, msg):
    for item in data["mailbox"]:
        if item is msg:
            break
        if msg["mode"] != "send" and item["status"] == "queued":
            item["status"] = "superseded"
    msg["status"] = "cancelled" if msg["mode"] == "cancel" else "submitted"


def receipt_line(data):
    print(f"Claude {data['name']}: {data['status']} — claude_code.show({data['id']!r})", flush=True)


async def work(directory, token):
    with change(directory) as data:
        if data["launch_token"] != token or data["status"] != "starting":
            return
        data["status"] = "running"
    # Only an explicitly recorded False selects the shared tool surface on resume.
    read_only = data.get("read_only") is not False
    sources = [] if read_only else ["user", "project", "local"]
    path, env = await asyncio.to_thread(installed_claude, data["cwd"], data.get("config_dir"), sources)
    # `env` only adds/overrides in the SDK, so remove ambient credentials at source.
    state_root = str(directory.parent)
    os.environ.clear()
    os.environ.update(env)
    os.environ["PRIME_CLAUDE_STATE"] = state_root
    claude_id = data["claude_session"]
    projects = (Path(data["cwd"]) / Path(env.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()) / "projects"
    def find_transcript():
        matches = [p for p in projects.glob(f"*/{claude_id}.jsonl") if p.is_file()]
        return str(matches[0]) if len(matches) == 1 else None
    if data["native_started"] and not find_transcript():
        raise RuntimeError("Native transcript missing or ambiguous; refusing to start a substitute session")
    extra = {"safe-mode": None, "strict-mcp-config": None,
             "resume" if data["native_started"] else "session-id": claude_id}
    if not read_only:
        extra["permission-prompts"] = "none"
    role = ("You are a read-only worker. " if read_only else
            "Work in the shared checkout. For reviews, inspect without changing files; "
            "when asked to implement, edit and test in place. ")
    diagnostics = directory / "stderr.log"
    def stderr(line):
        with diagnostics.open("a") as fd:
            fd.write(line + "\n")

    opts = ClaudeAgentOptions(cli_path=path, cwd=data["cwd"], model=data["requested_model"],
                              tools=list(TOOLS) if read_only else {"type": "preset", "preset": "claude_code"},
                              allowed_tools=list(TOOLS) if read_only else [],
                              permission_mode="dontAsk" if read_only else "default",
                              setting_sources=sources, max_turns=30, system_prompt={"type": "preset",
                                  "preset": "claude_code", "append": role + GUIDANCE},
                              extra_args=extra, stderr=stderr)
    initialized = False
    async with ClaudeSDKClient(options=opts) as client:
        while True:
            with change(directory) as data:
                msg = queued(data)
                if not msg:
                    if data["status"] == "running":
                        data["status"] = "idle"
                    receipt_line(data)
                    return
                select(data, msg)
                if msg["mode"] == "cancel":
                    data["status"] = "cancelled"
                    continue
                data["status"] = "running"
                data["activity"] = {"kind": "starting_turn", "at": time.time()}
            await client.query(msg["prompt"])
            terminal = None
            interrupted = False
            latest_limits = None
            async def receive():
                nonlocal terminal, latest_limits, initialized
                async for event in client.receive_response():
                    if isinstance(event, SystemMessage) and event.subtype == "init":
                        actual = event.data
                        if (actual.get("session_id") != claude_id or actual.get("mcp_servers")
                                or (read_only and set(actual.get("tools", [])) - set(TOOLS))
                                or actual.get("permissionMode") not in (
                                    {"dontAsk"} if read_only else {"default", "manual"})):
                            raise RuntimeError("Unexpected Claude session, tools, MCP or permission mode")
                        initialized = True
                        with change(directory) as state:
                            state.update(native_started=True, transcript=find_transcript(),
                                         model=actual.get("model"), claude_version=actual.get("claude_code_version"))
                    elif isinstance(event, AssistantMessage):
                        tools = [block.name for block in event.content if isinstance(block, ToolUseBlock)]
                        with change(directory) as state:
                            state["model"] = event.model
                            state["activity"] = {"kind": "tools" if tools else "responding", "tools": tools,
                                                 "at": time.time()}
                    elif isinstance(event, RateLimitEvent):
                        latest_limits = cache_usage(event)
                    elif isinstance(event, ResultMessage):
                        terminal = event
            task = asyncio.create_task(receive())
            lease_at = time.monotonic()
            try:
                while not task.done():
                    await asyncio.wait({task}, timeout=0.2)
                    if task.done() or interrupted:
                        continue
                    state = read(directory / "state.json")
                    if state.get("agents_view_dir") and time.monotonic() - lease_at >= 10:
                        with change(directory) as latest:
                            latest["agents_view_lease_at"] = time.time()
                        lease_at = time.monotonic()
                    next_msg = queued(state)
                    if next_msg and next_msg["mode"] in {"redirect", "cancel"}:
                        await asyncio.wait_for(client.interrupt(), timeout=12)
                        interrupted = True
                        await asyncio.wait_for(task, timeout=15)
                await task
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            if terminal is None or terminal.session_id != claude_id:
                raise RuntimeError("No matching terminal result from Claude")
            outcome = (terminal.result or "").strip()
            error = terminal.is_error or terminal.subtype != "success"
            limit_hit = terminal.api_error_status == 429 or (latest_limits or {}).get("status") == "rejected"
            with change(directory) as state:
                if not initialized:
                    raise RuntimeError("Claude did not report validated session initialization for this connection")
                now = time.time()
                if terminal.terminal_reason in {"aborted_streaming", "aborted_tools"}:
                    final = "interrupted"
                elif limit_hit and error:
                    final = "quota_blocked"
                elif error:
                    final = "error"
                elif outcome.startswith("BLOCKED:") or terminal.permission_denials:
                    final = "blocked"
                else:
                    final = "idle"
                item = next(m for m in state["mailbox"] if m["id"] == msg["id"])
                item["status"] = "interrupted" if final == "interrupted" else "answered" if not error else "failed"
                state["handoffs"].append(dict(at=now, message=msg["id"], status=final, result=outcome[:16_000],
                                               terminal_reason=terminal.terminal_reason,
                                               interrupt_requested=interrupted, model=state.get("model"),
                                               cache={k: terminal.usage[k] for k in ("cache_read_input_tokens",
                                                       "cache_creation_input_tokens", "input_tokens")
                                                       if terminal.usage and k in terminal.usage},
                                               quota_reset_at=(latest_limits or {}).get("resetsAt") if limit_hit else None,
                                               errors=(terminal.errors or [])[:3]))
                state.update(updated_at=now, last_result_at=now, activity=None,
                             transcript=find_transcript(), last_error=None)
                waiting = queued(state)
                if final in {"quota_blocked", "error", "blocked"} or not waiting:
                    state["status"] = final
                    # Don't strand or replay queued work silently on a later invocation.
                    for item in state["mailbox"]:
                        if item["status"] == "queued":
                            item["status"] = "not_run"
                    receipt_line(state)
                    return


async def main(directory, token):
    with lock(directory / "worker.lock"):
        try:
            async with asyncio.timeout(900):
                await work(directory, token)
        except Exception as exc:
            with change(directory) as state:
                if state["launch_token"] != token or state["status"] != "running":
                    return 1
                state.update(status="error", last_error=f"{type(exc).__name__}: {exc}"[:1800],
                             updated_at=time.time(), activity=None)
                for msg in state["mailbox"]:
                    if msg["status"] in {"queued", "submitted"}:
                        msg["status"] = "unknown_after_exit" if msg["status"] == "submitted" else "not_run"
                receipt_line(state)
            return 1
    return 0


if __name__ == "__main__":
    os.umask(0o077)
    os.environ["PRIME_CLAUDE_STATE"] = sys.argv[1]
    sys.exit(asyncio.run(main(job_dir(sys.argv[2]), sys.argv[3])))
