import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import claude_code as c
from claude_code import worker


class ConnectorTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.cwd = Path(temp.name).resolve()
        self.env = patch.dict(os.environ, {"PRIME_CLAUDE_STATE": str(self.cwd / "state")})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_private_staging_no_prompt_in_command(self):
        job = c.start("unique brief", cwd=self.cwd)
        self.assertNotIn("unique brief", job["command"])
        self.assertEqual(c.history(query="unique")[0]["id"], job["id"])
        self.assertEqual(c.job_dir(job["id"]).stat().st_mode & 0o777, 0o700)

    def test_agents_projection_is_opt_in_private_and_tracks_exchange(self):
        agent_dir = self.cwd / "agent"
        with patch.dict(os.environ, {"PRIME_AGENT_CODING_AGENT_DIR": str(agent_dir),
                                     "RLM_SESSION_ID": "saved-owner"}):
            c.start("private by default", cwd=self.cwd)
            self.assertFalse((agent_dir / "external-collaborators").exists())
            job = c.start("check the patch", cwd=self.cwd, name="review", agents_view=True)
            projected = agent_dir / "external-collaborators" / "saved-owner" / (job["id"] + ".json")
            preview = c.read(projected)
            self.assertEqual(preview["owner_session_id"], "saved-owner")
            self.assertEqual(preview["messages"][0]["status"], "queued")
            self.assertEqual(projected.stat().st_mode & 0o777, 0o600)
            with c.change(c.job_dir(job["id"])) as state:
                state["status"] = "blocked"
                state["mailbox"][0]["status"] = "answered"
                state["handoffs"].append({"at": time.time(), "status": "blocked", "result": "BLOCKED: \x1bWhich patch?"})
            preview = c.read(projected)
            self.assertEqual(preview["status"], "blocked")
            self.assertEqual([m["direction"] for m in preview["messages"]], ["to_external", "from_external"])
            self.assertEqual(preview["messages"][-1]["text"], "BLOCKED: Which patch?")

    def test_agents_projection_respects_local_preference_and_override(self):
        agent_dir = self.cwd / "agent"
        agent_dir.mkdir()
        (agent_dir / "claude-agents-view.enabled").touch()
        with patch.dict(os.environ, {"PRIME_AGENT_CODING_AGENT_DIR": str(agent_dir),
                                     "RLM_SESSION_ID": ""}):
            c.start("unsaved", cwd=self.cwd)
            rows = agent_dir / "external-collaborators" / "saved-owner"
            self.assertFalse(rows.exists())
            with patch.dict(os.environ, {"RLM_SESSION_ID": "saved-owner"}):
                c.start("no preview", cwd=self.cwd, agents_view=False)
                self.assertFalse(rows.exists())
                job = c.start("automatic preview", cwd=self.cwd)
                self.assertTrue((rows / (job["id"] + ".json")).is_file())

    def test_projection_marks_truncation_within_reader_limit(self):
        from claude_code.agents_view import MAX_TEXT, clipped

        self.assertEqual(clipped("a" * MAX_TEXT, MAX_TEXT), "a" * MAX_TEXT)
        preview = clipped("a" * (MAX_TEXT + 1), MAX_TEXT)
        self.assertEqual(len(preview), MAX_TEXT)
        self.assertTrue(preview.endswith("…"))

    def test_resume_preserves_id_and_cwd(self):
        job = c.start("first", cwd=self.cwd)
        path = c.job_dir(job["id"])
        with c.change(path) as data:
            data["status"] = "idle"
            data["mailbox"][0]["status"] = "answered"
            original = data["claude_session"]
        resumed = c.send(job["id"], "second")
        self.assertIsNotNone(resumed["command"])
        self.assertEqual(c.show(job["id"])["claude_session"], original)
        self.assertEqual(c.show(job["id"])["cwd"], str(self.cwd))

    def run_permission_turn(self, job, mode, tools, *, denied=False):
        directory = c.job_dir(job["id"])
        state = c.read(directory / "state.json")
        session = state["claude_session"]
        config = self.cwd / "claude"
        transcript = config / "projects" / "test" / f"{session}.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.touch()

        async def response():
            yield worker.SystemMessage(subtype="init", data={"session_id": session, "permissionMode": mode,
                                                           "tools": tools, "mcp_servers": []})
            yield worker.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, num_turns=1,
                                       session_id=session, is_error=False, result="Finished turn",
                                       permission_denials=[{"tool_name": "Edit"}] if denied else [])

        with (patch.dict(os.environ),
              patch.object(worker, "installed_claude", return_value=("/local/claude", {"CLAUDE_CONFIG_DIR": str(config)})),
              patch.object(worker, "ClaudeSDKClient", autospec=True) as client_type):
            client = client_type.return_value.__aenter__.return_value
            client.receive_response = response
            asyncio.run(worker.work(directory, state["launch_token"]))
            return client_type.call_args.kwargs["options"]

    def test_native_permissions_on_start_and_resume(self):
        job = c.start("review", cwd=self.cwd)
        session = c.show(job["id"])["claude_session"]
        for resume in (False, True):
            if resume:
                c.send(job["id"], "implement the review")
            options = self.run_permission_turn(job, "default", ["Read", "Edit", "Bash"], denied=resume)
            self.assertEqual(options.extra_args["resume" if resume else "session-id"], session)
            self.assertEqual(options.permission_mode, "default")
            self.assertFalse(options.allowed_tools)
            self.assertEqual(set(options.setting_sources), {"user", "project", "local"})
            self.assertEqual(options.extra_args["permission-prompts"], "none")
            self.assertIsNone(options.can_use_tool)
            self.assertTrue(options.tools is None or options.tools == {"type": "preset", "preset": "claude_code"})
            self.assertNotIn("dangerously-skip-permissions", options.extra_args)
            self.assertEqual(c.show(job["id"])["status"], "blocked" if resume else "idle")

    def test_read_only_resume_without_access_field_stays_restricted(self):
        job = c.start("review", cwd=self.cwd, read_only=True)
        tools = ["Read", "Glob", "Grep"]
        for legacy in (False, True):
            if legacy:
                with c.change(c.job_dir(job["id"])) as state:
                    del state["read_only"]
                c.send(job["id"], "now implement it")
            options = self.run_permission_turn(job, "dontAsk", tools)
            self.assertEqual(set(options.tools), set(tools))
            self.assertEqual(set(options.allowed_tools), set(tools))
            self.assertEqual(options.permission_mode, "dontAsk")

    def test_active_vs_lost_worker(self):
        job = c.start("first", cwd=self.cwd)
        path = c.job_dir(job["id"])
        with c.change(path) as data:
            data["status"] = "running"
            data["mailbox"][0]["status"] = "submitted"
        with c.lock(path / "worker.lock"):
            self.assertIsNone(c.send(job["id"], "second")["command"])
        self.assertEqual(c.show(job["id"])["status"], "interrupted_or_unknown")
        c.send(job["id"], "explicit recovery")
        self.assertEqual([m["status"] for m in c.show(job["id"])["mailbox"]],
                         ["unknown_after_exit", "unknown_after_exit", "queued"])

    def test_redirect_supersedes_older_queued_work(self):
        data = {"mailbox": []}
        first = c.enqueue(data, "send", "first")
        steer = c.enqueue(data, "redirect", "steer")
        self.assertIs(worker.queued(data), steer)
        worker.select(data, steer)
        self.assertEqual(first["status"], "superseded")
        self.assertEqual(steer["status"], "submitted")

    def test_partial_usage_does_not_refresh_other_window(self):
        def event(raw):
            return SimpleNamespace(rate_limit_info=SimpleNamespace(raw=raw))
        with patch("time.time", return_value=time.time() - c.STALE_SECONDS - 5):
            worker.cache_usage(event({"rateLimitType": "five_hour", "utilization": 0.3}))
        worker.cache_usage(event({"rateLimitType": "seven_day", "utilization": 0.1}))
        windows = c.usage()["windows"]
        self.assertEqual(windows["five_hour"]["used_fraction"], 0.3)
        self.assertTrue(windows["five_hour"]["stale"])
        self.assertFalse(windows["seven_day"]["stale"])

    def test_fails_closed_without_subscription_and_strips_api_env(self):
        with (patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fake", "ANTHROPIC_BASE_URL": "fake"}),
              patch.object(worker.shutil, "which", return_value="/local/claude"),
              patch.object(worker.subprocess, "run", return_value=SimpleNamespace(returncode=0,
                   stdout=json.dumps({"loggedIn": True, "authMethod": "api_key", "apiProvider": "firstParty"}))) as run):
            with self.assertRaisesRegex(RuntimeError, "no API auth fallback"):
                worker.installed_claude(self.cwd)
            self.assertNotIn("ANTHROPIC_API_KEY", run.call_args.kwargs["env"])
            self.assertNotIn("ANTHROPIC_BASE_URL", run.call_args.kwargs["env"])

    def test_preserves_implicit_default_login(self):
        result = SimpleNamespace(returncode=0, stdout=json.dumps({"loggedIn": True,
                                 "authMethod": "claude.ai", "apiProvider": "firstParty"}))
        with (patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/ambient"}),
              patch.object(worker.shutil, "which", return_value="/local/claude"),
              patch.object(worker.subprocess, "run", return_value=result)):
            self.assertNotIn("CLAUDE_CONFIG_DIR", worker.installed_claude(self.cwd, None)[1])
            self.assertEqual(worker.installed_claude(self.cwd, "/recorded")[1]["CLAUDE_CONFIG_DIR"], "/recorded")

    def test_supervised_completion_uses_current_handoff_only(self):
        callbacks = []
        shell = lambda command: SimpleNamespace(set_claude_completion=callbacks.append)
        with patch.dict(os.environ, {"PRIME_AGENT_CODING_AGENT_DIR": str(self.cwd),
                                     "RLM_SESSION_ID": "session"}):
            job = c.start("private brief", cwd=self.cwd, name="patch", shell=shell, agents_view=True)
        directory = c.job_dir(job["id"])
        with c.change(directory) as state:
            state["status"] = "blocked"
            state["handoffs"].append(dict(at=time.time(), status="blocked", model="verified-model",
                                          result="BLOCKED: \x1b" + "x" * 500))
        result = callbacks[0](0)
        preview = result.pop("preview")
        self.assertTrue(preview.startswith("BLOCKED: x"))
        self.assertLessEqual(len(preview), 240)
        self.assertEqual(result, {"id": job["id"], "name": "patch", "status": "blocked",
                                  "model": "verified-model"})
        c.send(job["id"], "follow-up", shell=shell)
        with self.assertRaisesRegex(ValueError, "superseded"):
            callbacks[0](0)
        self.assertEqual(callbacks[1](1), {"id": job["id"], "name": "patch", "status": "failed"})

    def test_completion_without_preview_and_stock_fallback(self):
        callbacks = []
        handle = SimpleNamespace(set_claude_completion=callbacks.append)
        job = c.start("private brief", cwd=self.cwd, shell=lambda command: handle, agents_view=False)
        self.assertIs(job["handle"], handle)
        directory = c.job_dir(job["id"])
        for state_status, expected in [("idle", "ready"), ("quota_blocked", "blocked"), ("error", "failed")]:
            with c.change(directory) as state:
                state["status"] = state_status
                state["handoffs"].append(dict(at=time.time(), status=state_status, result="private result"))
            self.assertEqual(callbacks[0](0), {"id": job["id"], "name": job["name"], "status": expected})
        stock = object()
        self.assertIs(c.send(job["id"], "resume", shell=lambda command: stock)["handle"], stock)

    def test_history_across_worktrees(self):
        main = self.cwd / "main"
        other = self.cwd / "other"
        meta = main / ".git" / "worktrees" / "other"
        meta.mkdir(parents=True)
        other.mkdir()
        (other / ".git").write_text(f"gitdir: {meta}\n")
        (meta / "commondir").write_text("../..\n")
        job = c.start("patch review", cwd=main)
        self.assertEqual(c.history(repo=other, query="patch")[0]["id"], job["id"])
