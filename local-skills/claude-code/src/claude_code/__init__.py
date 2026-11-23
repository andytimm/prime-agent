"""Local handoffs and mailbox for the Prime-supervised Claude worker."""
import contextlib
import fcntl
import json
import os
import shlex
import time
import uuid
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
TOOLS = ("Read", "Glob", "Grep")
STALE_SECONDS = 900


def root():
    path = Path(os.environ.get("PRIME_CLAUDE_STATE", Path.home() / ".prime/agent/claude-workers")).expanduser().resolve()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_mode & 0o077:
        raise PermissionError(f"Worker history must be private (chmod 700): {path}")
    return path


def read(path):
    return json.loads(path.read_text())


def write(path, value):
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(value, out, ensure_ascii=False)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


@contextlib.contextmanager
def lock(path, *, blocking=True):
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "r"):
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield


def job_dir(job):
    return root() / str(uuid.UUID(job))


@contextlib.contextmanager
def change(directory):
    with lock(directory / "state.lock"):
        path = directory / "state.json"
        data = read(path)
        yield data
        write(path, data)
        from .agents_view import publish
        publish(data)


def repo_key(cwd):
    for p in (cwd, *cwd.parents):
        marker = p / ".git"
        if marker.is_dir():
            return str(marker.resolve())
        if marker.is_file():
            line = marker.read_text().strip()
            if line.startswith("gitdir: "):
                gd = (p / line[8:]).resolve()
                common = gd / "commondir"
                return str((gd / common.read_text().strip()).resolve() if common.is_file() else gd)
    return str(cwd)


def receipt(data, *, command=False, message=None):
    cmd = ["uv", "run", "--locked", "--project", str(PROJECT), "python", "-m", "claude_code.worker",
           str(root()), data["id"], data["launch_token"]] if command else None
    return {"id": data["id"], "name": data["name"], "status": data["status"],
            "message": message["id"] if message else None,
            "delivery": message["status"] if message else None,
            "command": "exec " + shlex.join(cmd) if cmd else None}


def enqueue(data, mode, prompt):
    if not isinstance(prompt, str) or (mode != "cancel" and not prompt.strip()):
        raise ValueError("A nonempty text brief is required")
    if len(prompt) > 24_000:
        raise ValueError("Keep the brief under 24,000 characters")
    item = {"id": uuid.uuid4().hex[:12], "mode": mode, "prompt": prompt,
            "status": "queued", "at": time.time()}
    data["mailbox"].append(item)
    data["updated_at"] = time.time()
    return item


def launch(receipt, shell, directory, data):
    if shell and receipt["command"]:
        token = data["launch_token"]
        first_handoff = len(data["handoffs"])
        identity = {"id": data["id"], "name": data["name"]}

        def completion(exit_code: int) -> dict:
            from .agents_view import clipped
            state = read(directory / "state.json")  # atomic worker snapshot, not the UI projection
            if state["launch_token"] != token:
                raise ValueError("Claude launch was superseded")
            status = {"idle": "ready", "blocked": "blocked", "quota_blocked": "blocked"}.get(
                state["status"], "failed") if exit_code == 0 else "failed"
            result = {**identity, "status": status}
            if len(state["handoffs"]) > first_handoff:
                handoff = state["handoffs"][-1]
                if handoff["status"] == state["status"] and not exit_code:
                    if model := handoff.get("model"):
                        result["model"] = model
                    if state.get("agents_view_dir") and handoff.get("result"):
                        result["preview"] = clipped(handoff["result"], 240)
            return result

        handle = receipt["handle"] = shell(receipt["command"])
        if hasattr(handle, "set_claude_completion"):
            handle.set_claude_completion(completion)
    return receipt


def start(prompt, *, cwd, name="review", model=None, shell=None, agents_view=None, read_only=False):
    """Start Claude in this checkout; read_only pins the job to Read, Glob and Grep."""
    if not isinstance(read_only, bool):
        raise TypeError("read_only must be a bool")
    from .agents_view import default_enabled, destination, publish
    if agents_view is None:
        agents_view = default_enabled()
    cwd = Path(cwd).expanduser().resolve(strict=True)
    if not cwd.is_dir():
        raise NotADirectoryError(cwd)
    now = time.time()
    job = str(uuid.uuid4())
    data = dict(id=job, name=name, cwd=str(cwd), repo=repo_key(cwd), requested_model=model,
                read_only=read_only, config_dir=os.environ.get("CLAUDE_CONFIG_DIR"),
                prime_session=os.environ.get("RLM_SESSION_ID"), claude_session=str(uuid.uuid4()),
                agents_view_dir=destination() if agents_view else None,
                launch_token=uuid.uuid4().hex, started_at=now, updated_at=now, last_result_at=None,
                status="starting", mailbox=[], handoffs=[], native_started=False)
    msg = enqueue(data, "send", prompt)
    directory = job_dir(job)
    directory.mkdir(mode=0o700)
    write(directory / "state.json", data)
    publish(data)
    return launch(receipt(data, command=True, message=msg), shell, directory, data)


def observed_status(directory, data):
    if data["status"] not in {"running", "starting"}:
        return data["status"]
    try:
        with lock(directory / "worker.lock", blocking=False):
            if data["status"] == "starting" and time.time() - data["updated_at"] < 30:
                return "starting"
            return "interrupted_or_unknown"
    except BlockingIOError:
        return data["status"]


def _message(job, prompt, mode, shell):
    directory = job_dir(job)
    with change(directory) as data:
        status = observed_status(directory, data)
        needs_launch = status not in {"running", "starting"}
        if mode == "cancel" and needs_launch:
            raise RuntimeError("No active worker to cancel")
        if status == "interrupted_or_unknown":
            for prior in data["mailbox"]:
                if prior["status"] in {"queued", "submitted"}:
                    prior["status"] = "unknown_after_exit"
        msg = enqueue(data, mode, prompt)
        if needs_launch:
            data.update(status="starting", launch_token=uuid.uuid4().hex)
        out = receipt(data, command=needs_launch, message=msg)
    return launch(out, shell, directory, data)


def send(job, prompt, *, shell=None):
    """Queue a follow-up, or resume the exact Claude session if idle."""
    return _message(job, prompt, "send", shell)


def redirect(job, prompt, *, shell=None):
    """Interrupt the active turn and send a correction; not a rollback."""
    return _message(job, prompt, "redirect", shell)


def cancel(job):
    """Request active turn cancellation and discard earlier queued messages."""
    return _message(job, "", "cancel", None)


def show(job, *, limit=3):
    """Read a local handoff, delivery status, native transcript and cache observations."""
    directory = job_dir(job)
    data = read(directory / "state.json")
    data["status"] = observed_status(directory, data)
    data.pop("launch_token")
    data.setdefault("read_only", True)
    data["mailbox"] = [{k: v for k, v in m.items() if k != "prompt"} for m in data["mailbox"][-12:]]
    data["handoffs"] = data["handoffs"][-max(1, min(limit, 20)):]
    data["idle_seconds_since_result"] = (round(time.time() - data["last_result_at"])
                                           if data["last_result_at"] is not None and
                                           data["status"] not in {"running", "starting"} else None)
    if data.get("native_started"):
        data["transcript_available"] = bool(data.get("transcript") and Path(data["transcript"]).is_file())
    return data


def history(*, repo=None, query="", limit=10):
    """Find earlier delegations across conversations/worktrees, offline."""
    key = repo_key(Path(repo).expanduser().resolve()) if repo else None
    rows = []
    for path in root().glob("*/state.json"):
        data = read(path)
        brief = data["mailbox"][0]["prompt"]
        result = (data["handoffs"][-1].get("result") or "") if data["handoffs"] else ""
        haystack = " ".join([data["name"], data["cwd"],
                             *(m["prompt"] for m in data["mailbox"]),
                             *(h.get("result") or "" for h in data["handoffs"])]).casefold()
        if (not key or key == data["repo"]) and all(word in haystack for word in query.casefold().split()):
            row = {k: data[k] for k in ("id", "name", "cwd", "updated_at")}
            row.update(status=observed_status(path.parent, data), brief=brief[:220], outcome=result[:220])
            rows.append(row)
    return sorted(rows, key=lambda row: row["updated_at"], reverse=True)[:max(1, min(limit, 50))]


def usage():
    """Last Claude-reported subscription windows; never contacts Claude."""
    path = root() / "usage.json"
    if not path.is_file():
        return {"status": "unknown", "windows": {}}
    data = read(path)
    now = time.time()
    data["age_seconds"] = round(now - data["observed_at"])
    data["status"] = "stale" if data["age_seconds"] > STALE_SECONDS else "last_known"
    for window in data["windows"].values():
        window["age_seconds"] = round(now - window["observed_at"])
        window["stale"] = (window["age_seconds"] > STALE_SECONDS or
                           (window["resets_at"] is not None and window["resets_at"] <= now))
    return data
