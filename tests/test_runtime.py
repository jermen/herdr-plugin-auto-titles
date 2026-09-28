import copy
import io
import json
import os
from pathlib import Path
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from auto_titles import Contexts, Tickets, apply, call, load_state, restore
from conversation import ConversationTitles
from titles import Planner, configuration

ROOT = Path(__file__).resolve().parents[1]


class FakeHerdr(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path):
        self.snapshot = {"panes": [{"pane_id": "w1:p1", "tab_id": "w1:t1", "terminal_id": "term1",
                                     "cwd": "/a/Company/Dev", "agent": "codex", "terminal_title_stripped": "OPS-1425: New plugin | Dev"}],
                         "tabs": [{"tab_id": "w1:t1", "workspace_id": "w1", "number": 50, "label": "1"}]}
        self.calls = []

        class Handler(socketserver.StreamRequestHandler):
            def handle(inner):
                request = json.loads(inner.rfile.readline())
                method, params = request["method"], request["params"]
                self.calls.append((method, params))
                if method == "session.snapshot":
                    result = {"snapshot": copy.deepcopy(self.snapshot)}
                elif method == "pane.process_info":
                    result = {"foreground_process_group_id": 42, "foreground_processes": [{"pid": 99, "cwd": "/tmp/mcp"}, {"pid": 42, "cwd": "/a/Company/Dev"}]}
                else:
                    kind, action = method.split(".")
                    obj = next(o for o in self.snapshot[kind + "s"] if o[kind + "_id"] == params[kind + "_id"])
                    if action == "rename":
                        obj["label"] = params["label"]
                    result = {kind: obj}
                inner.wfile.write((json.dumps({"id": request["id"], "result": result}) + "\n").encode())

        super().__init__(str(path), Handler)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.endpoint = str(self.root / "herdr.sock")
        self.server = FakeHerdr(self.endpoint)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.config = configuration({"poll_seconds": 0.5})
        self.state_path = self.root / "ownership.json"
        self.env = os.environ | {"HERDR_SOCKET_PATH": self.endpoint, "HERDR_PLUGIN_STATE_DIR": str(self.root / "state"),
                                 "HERDR_PLUGIN_CONFIG_DIR": str(self.root / "config"), "PYTHONDONTWRITEBYTECODE": "1"}
        (self.root / "config").mkdir()
        (self.root / "config/config.json").write_text(json.dumps({"poll_seconds": 0.5}))

    def tearDown(self):
        self.run_cli("stop")
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def run_cli(self, cmd):
        return subprocess.run([sys.executable, str(ROOT / "auto_titles.py"), cmd], env=self.env, text=True, capture_output=True, timeout=20, check=True)

    def wait_for(self, predicate):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail("timed out waiting for watcher")

    def plan(self):
        snap = call(self.endpoint, "session.snapshot")["snapshot"]
        contexts = Contexts(self.endpoint, self.config)
        with patch.object(contexts.branches, "get", return_value=""):
            titles = contexts.read(snap)
        planner = Planner(self.config, load_state(self.state_path, self.endpoint))
        return planner, planner.plan(snap, titles)

    def test_apply_then_restore_preserves_manual_edits(self):
        planner, changes = self.plan()
        apply(self.endpoint, planner, changes, self.state_path)
        self.assertEqual(self.server.snapshot["tabs"][0]["label"], "1 · OPS-1425: New plugin")
        self.server.snapshot["panes"][0]["label"] = "Manual rename"
        restore(self.endpoint, self.state_path)
        self.assertEqual(self.server.snapshot["tabs"][0]["label"], "1")
        self.assertEqual(self.server.snapshot["panes"][0]["label"], "Manual rename")

    def test_manual_edit_between_snapshot_and_write(self):
        planner, changes = self.plan()
        self.server.snapshot["tabs"][0]["label"] = "Manual edit"
        apply(self.endpoint, planner, changes, self.state_path)
        self.assertEqual(self.server.snapshot["tabs"][0]["label"], "Manual edit")

    def test_restore_retains_manual_tab_renamed_after_automatic_title(self):
        planner, changes = self.plan()
        apply(self.endpoint, planner, changes, self.state_path)
        self.server.snapshot["tabs"][0]["label"] = "New manual window"
        planner, changes = self.plan()
        apply(self.endpoint, planner, changes, self.state_path)
        self.assertEqual(self.server.snapshot["tabs"][0]["label"], "1 · New manual window")
        restore(self.endpoint, self.state_path)
        self.assertEqual(self.server.snapshot["tabs"][0]["label"], "New manual window")

    def test_restore_removes_only_our_prefix_from_existing_manual_tab(self):
        self.server.snapshot["tabs"][0]["label"] = "Existing manual window"
        planner, changes = self.plan()
        apply(self.endpoint, planner, changes, self.state_path)
        self.assertEqual(self.server.snapshot["tabs"][0]["label"], "1 · Existing manual window")
        restore(self.endpoint, self.state_path)
        self.assertEqual(self.server.snapshot["tabs"][0]["label"], "Existing manual window")

    def test_foreground_process_leader_wins_over_mcp_child(self):
        contexts = Contexts(self.endpoint, self.config)
        snap = copy.deepcopy(self.server.snapshot)
        snap["panes"][0].update(cwd="/old/Shell/Dev", foreground_cwd="/tmp/mcp", agent=None)
        with patch.object(contexts.branches, "get", return_value="") as branch:
            self.assertEqual(contexts.read(snap)["w1:p1"]["pane"], "Company")
            branch.assert_called_with("/a/Company/Dev")

    def test_preview_does_not_write_to_server_or_state(self):
        result = json.loads(self.run_cli("preview").stdout)
        self.assertEqual(len(result["changes"]), 2)
        self.assertFalse(any(m.endswith("rename") for m, _ in self.server.calls))
        self.assertFalse((self.root / "state").exists())

    def test_daemon_singleton_restart_and_restore(self):
        self.run_cli("start")
        self.wait_for(lambda: self.server.snapshot["tabs"][0]["label"] == "1 · OPS-1425: New plugin")
        self.wait_for(lambda: json.loads(self.run_cli("status").stdout).get("healthy"))
        first = json.loads(self.run_cli("status").stdout)
        self.run_cli("start")
        self.assertEqual(json.loads(self.run_cli("status").stdout)["pid"], first["pid"])
        self.run_cli("stop")
        self.assertFalse(json.loads(self.run_cli("status").stdout)["healthy"])
        self.server.snapshot["panes"][0]["terminal_title_stripped"] = "OPS-1425: Fix reconnects | Dev"
        self.run_cli("start")
        self.wait_for(lambda: self.server.snapshot["panes"][0]["label"] == "Fix reconnects")
        self.run_cli("restore")
        self.assertEqual(self.server.snapshot["tabs"][0]["label"], "1")
        self.assertEqual(self.server.snapshot["panes"][0].get("label"), "")

    def test_stale_server_identity_discards_old_ownership(self):
        self.state_path.write_text(json.dumps({"server": [0, 0], "owned": {"pane:w1:p1": {"original": "Wrong session"}}}))
        self.assertNotIn("owned", load_state(self.state_path, self.endpoint))

    def test_reported_titles_share_cadence_but_windows_update_promptly(self):
        now = [0]
        contexts = Contexts(self.endpoint, self.config, clock=lambda: now[0])
        snap = self.server.snapshot
        pane = snap["panes"][0]
        pane["title"] = "OPS-1425: Fix reconnect handling"
        pid = pane["pane_id"]
        with patch.object(contexts.branches, "get", return_value=""):
            self.assertEqual(contexts.read(snap)[pid]["pane"], "Fix reconnect handling")
            pane["title"] = "OPS-99: Add periodic title refresh"
            now[0] = 179
            result = contexts.read(snap)[pid]
            self.assertEqual(result["pane"], "Fix reconnect handling")
            self.assertEqual(result["window"], "OPS-99: Add periodic title refresh")
            now[0] = 180
            self.assertEqual(contexts.read(snap)[pid]["pane"], "Add periodic title refresh")
            pane["title"] = "OPS-99: add periodic title refresh!"
            now[0] = 360
            self.assertEqual(contexts.read(snap)[pid]["pane"], "Add periodic title refresh")
            pane["agent_session"] = {"kind": "id", "value": "new-session"}
            pane["title"] = "Fix deployment retries"
            self.assertEqual(contexts.read(snap)[pid]["pane"], "Fix deployment retries")
            pane["tokens"] = {"task_title": "Explicit task override"}
            self.assertEqual(contexts.read(snap)[pid]["pane"], "Explicit task override")
            pane["tokens"] = {}
            self.assertEqual(contexts.read(snap)[pid]["pane"], "Fix deployment retries")
            self.config["conversation_refresh_seconds"] = 0
            pane["title"] = "Add usage history"
            self.assertEqual(contexts.read(snap)[pid]["pane"], "Add usage history")
        contexts.read({"panes": []})
        self.assertEqual(contexts.pane_titles, {})

    def test_directory_fallback_can_be_replaced_immediately(self):
        now = [0]
        contexts = Contexts(self.endpoint, self.config, clock=lambda: now[0])
        snap = self.server.snapshot
        pane = snap["panes"][0]
        pane["terminal_title_stripped"] = ""
        pid = pane["pane_id"]
        with patch.object(contexts.branches, "get", return_value=""):
            fallback = contexts.read(snap)[pid]["pane"]
            pane["title"] = "Fix reconnect handling"
            now[0] = 2
            self.assertNotEqual(fallback, "Fix reconnect handling")
            self.assertEqual(contexts.read(snap)[pid]["pane"], "Fix reconnect handling")

    def test_conversation_refresh_reaches_pane_after_interval(self):
        pane = self.server.snapshot["panes"][0]
        pane["agent_session"] = {"kind": "id", "value": "test-session"}
        transcript = self.root / "codex/sessions/2026/09/21/rollout-test-session.jsonl"
        transcript.parent.mkdir(parents=True)

        def write_request(text, append=False):
            with transcript.open("a" if append else "w") as stream:
                if not append:
                    stream.write(json.dumps({"type": "session_meta", "payload": {"id": "test-session"}}) + "\n")
                stream.write(json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": text}}) + "\n")

        now = [0]
        contexts = Contexts(self.endpoint, self.config, clock=lambda: now[0])
        contexts.conversations = ConversationTitles({"codex": self.root / "codex"}, lambda: now[0])
        planner = Planner(self.config, load_state(self.state_path, self.endpoint))

        def refresh():
            snap = call(self.endpoint, "session.snapshot")["snapshot"]
            with patch.object(contexts.branches, "get", return_value=""):
                changes = planner.plan(snap, contexts.read(snap))
            apply(self.endpoint, planner, changes, self.state_path)

        write_request("Fix reconnect handling")
        refresh()
        self.assertEqual(pane["label"], "Fix reconnect handling")
        write_request("Add periodic title refresh", append=True)
        now[0] = 179
        refresh()
        self.assertEqual(pane["label"], "Fix reconnect handling")
        now[0] = 180
        refresh()
        self.assertEqual(pane["label"], "Add periodic title refresh")
        self.assertEqual(self.server.snapshot["tabs"][0]["label"], "1 · OPS-1425: New plugin")
        pane["label"] = "My manual name"
        write_request("Fix another issue", append=True)
        now[0] = 360
        refresh()
        self.assertEqual(pane["label"], "My manual name")


class TicketTests(unittest.TestCase):
    def setUp(self):
        self.config = configuration({"jira": [{"url": "https://jira.example", "projects": ["OPS"], "email": "test@example.com", "token_env": "TEST_JIRA_TOKEN"}]})

    def test_jira_is_opt_in_and_project_scoped(self):
        for config in (configuration({}), self.config):
            lookup = Tickets(config)
            with patch.object(lookup.opener, "open") as request:
                self.assertEqual(lookup.get("DMDOX-318"), "")
                request.assert_not_called()

    def test_cache_and_request_budget(self):
        lookup = Tickets(self.config)
        with patch.dict(os.environ, {"TEST_JIRA_TOKEN": "test-only"}), patch.object(lookup.opener, "open", return_value=io.BytesIO(b'{"fields":{"summary":"New plugin"}}')) as request:
            self.assertEqual(lookup.get("OPS-1425"), "New plugin")
            self.assertEqual(lookup.get("OPS-1425"), "New plugin")
            self.assertEqual(lookup.get("OPS-123"), "")
            self.assertEqual(request.call_count, 1)
            self.assertEqual(request.call_args.args[0].full_url, "https://jira.example/rest/api/3/issue/OPS-1425?fields=summary")

    def test_failure_does_not_erase_cached_summary(self):
        lookup = Tickets(self.config)
        lookup.cache[("https://jira.example", "test@example.com", "TEST_JIRA_TOKEN", "OPS-1425")] = (0, "Cached title")
        with patch.dict(os.environ, {"TEST_JIRA_TOKEN": "test-only"}), patch.object(lookup.opener, "open", side_effect=OSError("offline")):
            self.assertEqual(lookup.get("OPS-1425"), "Cached title")
            self.assertEqual(lookup.get("OPS-1425"), "Cached title")


if __name__ == "__main__":
    unittest.main()
