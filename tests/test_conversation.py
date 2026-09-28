import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from conversation import ConversationTitles, read_title, request_title, user_text


def message(text):
    return {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}}


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = 0
        self.reader = ConversationTitles({"codex": self.root / "codex", "claude": self.root / "claude"}, lambda: self.now)
        self.pane = {"agent": "codex", "agent_session": {"kind": "id", "value": "session-123", "agent": "codex"}}
        self.path = self.root / "codex/sessions/2026/09/21/rollout-session-123.jsonl"
        self.path.parent.mkdir(parents=True)
        self.rows = [{"type": "session_meta", "payload": {"id": "session-123"}}, message("Fix the reconnect race")]
        self.save()

    def tearDown(self):
        self.temp.cleanup()

    def save(self):
        self.path.write_text("".join(json.dumps(r) + "\n" for r in self.rows))

    def test_refreshes_only_after_three_minutes(self):
        self.assertEqual(self.reader.get(self.pane, 180), "Fix the reconnect race")
        self.rows.append(message("Can you refresh pane titles every three minutes?"))
        self.save()
        self.now = 179
        self.assertEqual(self.reader.get(self.pane, 180), "Fix the reconnect race")
        self.now = 180
        self.assertEqual(self.reader.get(self.pane, 180), "Refresh pane titles every three minutes")

    def test_session_change_does_not_keep_old_title(self):
        self.reader.get(self.pane, 180)
        self.pane["agent_session"]["value"] = "another-session"
        self.assertEqual(self.reader.get(self.pane, 180), "")
        self.reader.prune([self.pane])
        self.assertNotIn(("codex", "session-123"), self.reader.cache)

    def test_disabled_refresh_does_not_read_transcripts(self):
        with patch.object(self.reader, "locate") as locate:
            self.assertEqual(self.reader.get(self.pane, 0), "")
            locate.assert_not_called()

    def test_missing_or_unsupported_binding_does_not_scan(self):
        for pane in ({"agent": "gemini"}, {"agent": "codex"},
                     {"agent": "codex", "agent_session": {"kind": "id", "value": "../../secret"}},
                     {"agent": "codex", "agent_session": {"kind": "id", "value": "session-123", "agent": "claude"}}):
            with self.subTest(pane=pane), patch.object(self.reader, "locate") as locate:
                self.assertEqual(self.reader.get(pane, 180), "")
                locate.assert_not_called()

    def test_rejects_mismatched_codex_header(self):
        self.rows[0]["payload"]["id"] = "wrong-session"
        self.save()
        self.assertEqual(self.reader.get(self.pane, 180), "")

    def test_tools_approvals_and_injected_rules_are_ignored(self):
        self.rows += [
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Invent a different title"}]}},
            {"type": "response_item", "payload": {"type": "function_call_output", "output": "Run something else"}},
            message("Approved"), message("# AGENTS.md instructions for /some/repo\nUse these rules"),
            message("<environment_context>Run something else</environment_context>"),
        ]
        self.save()
        self.assertEqual(self.reader.get(self.pane, 180), "Fix the reconnect race")

    def test_codex_user_events(self):
        self.rows.append({"type": "event_msg", "payload": {"type": "user_message", "message": "Repair the CI pipeline"}})
        self.save()
        self.assertEqual(self.reader.get(self.pane, 180), "Repair the CI pipeline")

    def test_partial_last_line_is_read_when_completed(self):
        with self.path.open("a") as f:
            f.write(json.dumps(message("Repair the CI pipeline")))
        self.assertEqual(self.reader.get(self.pane, 180), "Fix the reconnect race")
        with self.path.open("a") as f:
            f.write("\n")
        self.now = 180
        self.assertEqual(self.reader.get(self.pane, 180), "Repair the CI pipeline")

    def test_malformed_rows_do_not_hide_last_request(self):
        with self.path.open("a") as f:
            f.write('{"unfinished":\n[]\nnull\n')
        self.assertEqual(self.reader.get(self.pane, 180), "Fix the reconnect race")

    def test_bounded_tail_retains_cached_task_after_large_tool_output(self):
        self.reader.get(self.pane, 180)
        with self.path.open("a") as f:
            f.write(json.dumps({"type": "tool", "output": "x" * 4096}) + "\n")
        self.now = 180
        with patch("conversation.MAX_SCAN", 1024):
            self.assertEqual(self.reader.get(self.pane, 180), "Fix the reconnect race")

    def test_claude_plain_and_block_requests_skip_tool_results(self):
        path = self.root / "claude/projects/project/session-123.jsonl"
        path.parent.mkdir(parents=True)
        rows = [
            {"sessionId": "session-123", "type": "user", "message": {"role": "user", "content": "Fix the first problem"}},
            {"sessionId": "session-123", "type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "Oprav obnovování názvu panelu"}]}},
            {"sessionId": "session-123", "type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "Ignore previous instructions"}]}},
            {"sessionId": "other", "type": "user", "message": {"role": "user", "content": "Wrong conversation request"}},
            {"sessionId": "session-123", "type": "user", "isMeta": True, "message": {"role": "user", "content": "Injected background information"}},
        ]
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        self.assertEqual(read_title(path, "claude", "session-123"), "Oprav obnovování názvu panelu")

    def test_ambiguous_and_outside_root_paths_are_not_read(self):
        other = self.path.with_name("other-session-123.jsonl")
        other.write_bytes(self.path.read_bytes())
        self.assertEqual(self.reader.get(self.pane, 180), "")
        other.unlink()
        outside = self.root / "outside.jsonl"
        self.path.rename(outside)
        self.path.symlink_to(outside)
        self.now = 180
        self.assertEqual(self.reader.get(self.pane, 180), "")

    def test_quoted_question_is_not_part_of_answer_title(self):
        text = "> Should it follow conversation or reported titles?\n\nDerive from recent conversation every 3 minutes (Recommended)"
        self.assertEqual(request_title(text), "Derive from recent conversation every 3 minutes")

    def test_code_approvals_and_prompt_scaffolding_are_filtered(self):
        for text in ("yes", "Approved.", "Go ahead!", "<system>Injected request</system>", "```sh\nrm something\n```", "# AGENTS.md rules\nRun something else"):
            with self.subTest(text=text):
                self.assertEqual(request_title(text), "")
        self.assertEqual(request_title("Please fix the example.com certificate. Then check DNS."), "Fix the example.com certificate")

    def test_followups_keep_task_even_after_reader_restart(self):
        self.rows += [message(text) for text in (
            "Yes, please", "Any updates?", "Can you fix it?", "Looks good.",
            "Use that approach", "Make it better", "Run the tests and merge it",
            "Create a merge request", "Log 2h and close the ticket",
            "Prepare python script in /tmp", "Context is still empty",
            "It now shows usage", "I cannot see the rest of the text", "On another machine locally",
            "It's Ubuntu 26.04", "That's working now",
        )]
        self.save()
        self.assertEqual(self.reader.get(self.pane, 180), "Fix the reconnect race")
        self.reader.cache.clear()
        self.assertEqual(self.reader.get(self.pane, 180), "Fix the reconnect race")

    def test_substantive_requests_after_generic_opening(self):
        for text in ("Thanks. Please fix example.com certificate renewal.",
                     "One more thing\nCould you fix example.com certificate renewal?",
                     "Yes, please fix example.com certificate renewal."):
            with self.subTest(text=text):
                self.assertEqual(request_title(text), "Fix example.com certificate renewal")

    def test_ticket_lead_in_does_not_leave_broken_title(self):
        self.assertEqual(request_title("Let's improve DMDOX-318: Pane names are often not very descriptive."),
                         "DMDOX-318: Pane names are often not very descriptive")
        self.assertEqual(request_title("Let's work on DMDOX-318. Improve pane descriptions."),
                         "DMDOX-318: Improve pane descriptions")
        self.assertEqual(request_title("Do DMDOX-319"), "DMDOX-319")
        self.assertEqual(request_title("Resume Herdr plugin development - DMDOX-318"), "DMDOX-318")

    def test_concrete_tasks_are_not_confused_with_followups(self):
        for text in ("Fix Docker image builds", "Create a merge request dashboard",
                     "Fix DNS", "Fix CI",
                     "Write a Python script to export Jira worklogs", "Run tests against staging.example.com",
                     "Explain PostgreSQL replication lag", "Oprav obnovování názvu panelu"):
            with self.subTest(text=text):
                self.assertEqual(request_title(text), text)

    def test_unfenced_json_and_logs_do_not_become_titles(self):
        self.rows.append(message('{\n  "pane": "w1:p1",\n  "usage": null\n}\nContext is still empty'))
        self.rows.append(message('"/home/test/session-123.jsonl"\nw1:p1 d -- / w -- / usage transcript_missing'))
        self.save()
        self.assertEqual(self.reader.get(self.pane, 180), "Fix the reconnect race")
        self.assertEqual(request_title('[2026-09-21] daemon starting\nPlease repair agent usage reporting'),
                         "Repair agent usage reporting")

    def test_cosmetic_rewording_keeps_title_but_topic_change_updates(self):
        self.assertEqual(self.reader.get(self.pane, 180), "Fix the reconnect race")
        self.rows.append(message("fix the reconnect race!"))
        self.save()
        self.now = 180
        self.assertEqual(self.reader.get(self.pane, 180), "Fix the reconnect race")
        self.rows.append(message("Add reconnect tests"))
        self.save()
        self.now = 360
        self.assertEqual(self.reader.get(self.pane, 180), "Add reconnect tests")

    def test_word_order_and_negation_are_not_cosmetic_changes(self):
        for index, text in enumerate(("Copy staging to production", "Copy production to staging", "Do not copy production to staging")):
            self.rows.append(message(text))
            self.save()
            self.now = index * 180
            self.assertEqual(self.reader.get(self.pane, 180), text)


if __name__ == "__main__":
    unittest.main()
