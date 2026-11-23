"""Bounded projection for the optional local Agents-view prototype."""
import os
import re
from pathlib import Path

from . import write

OWNER_ID = re.compile(r"[A-Za-z0-9_-]{1,96}")
MAX_MESSAGES = 8
MAX_TEXT = 1600


def default_enabled():
    directory = os.environ.get("PRIME_AGENT_CODING_AGENT_DIR")
    owner = os.environ.get("RLM_SESSION_ID", "")
    return bool(directory and OWNER_ID.fullmatch(owner)
                and (Path(directory) / "claude-agents-view.enabled").is_file())


def destination():
    directory = os.environ.get("PRIME_AGENT_CODING_AGENT_DIR")
    owner = os.environ.get("RLM_SESSION_ID", "")
    if not directory or not OWNER_ID.fullmatch(owner):
        raise RuntimeError("Agents-view publication requires a saved Prime session")
    return str(Path(directory).resolve() / "external-collaborators" / owner)


def clipped(text, limit):
    # Don't pass terminal escapes or C1 controls into a TUI. Preserve paragraphs and tabs.
    text = "".join(c for c in text[:limit + 1] if c in "\n\t" or ord(c) >= 32 and not 127 <= ord(c) <= 159)
    return text[:limit - 1] + "…" if len(text) > limit else text


def snapshot(data):
    messages = []
    for message in data["mailbox"][-MAX_MESSAGES:]:
        text = message["prompt"] or "Stop requested"
        messages.append((message["at"], {"direction": "to_external",
                         "status": message["status"], "text": clipped(text, MAX_TEXT)}))
    for result in data["handoffs"][-MAX_MESSAGES:]:
        if result["result"]:
            messages.append((result["at"], {"direction": "from_external",
                             "status": result["status"], "text": clipped(result["result"], MAX_TEXT)}))
    messages.sort(key=lambda item: item[0])
    activity = data.get("activity") or {}
    tool_names = activity.get("tools", [])
    label = ("Last observed: " + ", ".join(tool_names) if tool_names else
             {"starting_turn": "Starting turn", "responding": "Last observed: responding"}.get(activity.get("kind")))
    if data["status"] not in {"starting", "running"}:
        label = None
    return {"version": 1, "id": data["id"], "owner_session_id": data["prime_session"],
            "name": clipped(data["name"], 80), "provider": "Claude Code", "status": data["status"],
            "activity": clipped(label, 160) if label else None,
            "updated_at": max(data["updated_at"], activity.get("at", 0), data.get("agents_view_lease_at", 0)),
            "messages": [item[1] for item in messages[-MAX_MESSAGES:]]}


def publish(data):
    # Display data is best-effort, never a second mailbox or an execution channel.
    if parent := data.get("agents_view_dir"):
        directory = Path(parent)
        try:
            directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.mkdir(mode=0o700, exist_ok=True)
            if any(p.is_symlink() or p.stat().st_mode & 0o077 for p in (directory, directory.parent)):
                return
            write(directory / f"{data['id']}.json", snapshot(data))
        except OSError:
            # A missing or unwritable display projection must not kill a worker.
            return
