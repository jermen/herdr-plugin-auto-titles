import copy
import unittest

from titles import Planner, clean, configuration, directory_title, resolve, shorten


def pane(pid="w1:p1", **kw):
    return {"pane_id": pid, "tab_id": "w1:t1", "terminal_id": "term1", "cwd": "/a/Pražské Benátky s.r.o./Dev", **kw}


def snapshot(*panes):
    return {"panes": list(panes or [pane()]), "tabs": [{"tab_id": "w1:t1", "workspace_id": "w1", "number": 42, "label": "1"}]}


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        self.config = configuration({})

    def test_requested_directory_example(self):
        path = "/a/b/a/c/Pražské Benátky s.r.o./Dev/"
        self.assertEqual(directory_title(path, "#2"), "Pražské Benátky s.r.o.")
        self.assertEqual(directory_title(path, "#1"), "Dev")
        self.assertEqual(directory_title(path, "#3"), "c")
        self.assertEqual(directory_title("/a", "#99"), "a")
        self.assertEqual(directory_title("/", "#2"), "Terminal")

    def test_window_and_pane_have_different_titles(self):
        p = pane(agent="codex", terminal_title_stripped="OPS-1425: New plugin | Dev")
        self.assertEqual(resolve(p, self.config), {"pane": "New plugin", "window": "OPS-1425: New plugin", "ticket": "OPS-1425"})

    def test_pane_subtask_keeps_window_ticket_summary(self):
        p = pane(agent="claude", title="Fix reconnect race", tokens={"ticket_id": "DMDOX-318", "ticket_summary": "Automatic titles"})
        result = resolve(p, self.config)
        self.assertEqual(result["pane"], "Fix reconnect race")
        self.assertEqual(result["window"], "DMDOX-318: Automatic titles")

    def test_generic_ticket_prompt_uses_summary_for_pane(self):
        p = pane(agent="codex", terminal_title_stripped="Handle DMDOX-318 | Dev")
        result = resolve(p, self.config, summary=lambda k: "Automatic titles for windows and panes")
        self.assertEqual(result["pane"], "Automatic titles for windows and panes")
        self.assertEqual(result["window"], "DMDOX-318: Automatic titles for windows and panes")

    def test_config_summary_and_lowercase_branch(self):
        config = configuration({"ticket_titles": {"dmdox-318": "Automatic titles"}})
        result = resolve(pane(), config, "feature/dmdox-318")
        self.assertEqual(result["window"], "DMDOX-318: Automatic titles")
        self.assertEqual(result["pane"], "Automatic titles")

    def test_task_metadata_precedes_terminal(self):
        p = pane(agent="claude", title="Old task", terminal_title_stripped="Another task", tokens={"task_title": "Fix failing tests"})
        self.assertEqual(resolve(p, self.config)["pane"], "Fix failing tests")

    def test_conversation_updates_pane_but_preserves_window_context(self):
        p = pane(agent="codex", terminal_title_stripped="OPS-1425: New plugin | Dev", conversation_title="Fix reconnect handling")
        result = resolve(p, self.config)
        self.assertEqual(result["pane"], "Fix reconnect handling")
        self.assertEqual(result["window"], "OPS-1425: New plugin")
        p["tokens"] = {"task_title": "Explicit current task"}
        self.assertEqual(resolve(p, self.config)["pane"], "Explicit current task")

    def test_codex_attention_banner_is_not_task_context(self):
        for banner in ("[ ! ] Action Required | ", "Action Required | ", "Working | "):
            p = pane(agent="codex", terminal_title_stripped=banner + "Handle DMDOX-318 | Dev")
            result = resolve(p, self.config, summary=lambda k: "Automatic titles")
            self.assertEqual(result["pane"], "Automatic titles")
            self.assertEqual(result["window"], "DMDOX-318: Automatic titles")

    def test_ticket_only_conversation_keeps_summary_fallback(self):
        p = pane(agent="codex", terminal_title_stripped="Handle DMDOX-318 | Dev", conversation_title="Do DMDOX-318")
        self.assertEqual(resolve(p, self.config, summary=lambda k: "Automatic titles")["pane"], "Automatic titles")

    def test_concise_reported_title_must_match_current_request(self):
        p = pane(agent="codex", title="Improve pane name descriptions",
                 conversation_title="DMDOX-318: Pane names are often not very descriptive")
        self.assertEqual(resolve(p, self.config)["pane"], "Improve pane name descriptions")
        p["conversation_title"] = "Remove pane name descriptions"
        self.assertEqual(resolve(p, self.config)["pane"], "Remove pane name descriptions")
        p["title"] = "Add reconnect tests"
        p["conversation_title"] = "Fix reconnect handling"
        self.assertEqual(resolve(p, self.config)["pane"], "Fix reconnect handling")
        p["title"] = "Copy staging to production"
        p["conversation_title"] = "Copy production to staging"
        self.assertEqual(resolve(p, self.config)["pane"], "Copy production to staging")
        p["title"] = "Enable detailed debug logging"
        p["conversation_title"] = "Do not enable detailed debug logging"
        self.assertEqual(resolve(p, self.config)["pane"], "Do not enable detailed debug logging")
        p["title"] = "Fix profile validation"
        p["conversation_title"] = "Fix profile password validation"
        self.assertEqual(resolve(p, self.config)["pane"], "Fix profile password validation")

    def test_new_ticket_only_request_does_not_keep_old_task(self):
        self.config["ticket_titles"]["DMDOX-319"] = "Improve agent usage reporting"
        p = pane(agent="codex", title="DMDOX-318: Improve pane descriptions", conversation_title="DMDOX-319")
        result = resolve(p, self.config)
        self.assertEqual(result["pane"], "Improve agent usage reporting")
        self.assertEqual(result["window"], "DMDOX-318: Improve pane descriptions")

    def test_ticket_wrappers_and_branch_keep_useful_description(self):
        p = pane(agent="codex", title="Let's improve DMDOX-318: Pane descriptions")
        self.assertEqual(resolve(p, self.config)["pane"], "Pane descriptions")
        self.assertEqual(resolve(pane(), self.config, "DMDOX-318-pane-names")["pane"], "pane names")

    def test_long_titles_end_at_word_boundary(self):
        self.assertEqual(shorten("Repair certificate renewal for example.com", 32), "Repair certificate renewal for…")

    def test_titleless_ticket_branch(self):
        result = resolve(pane(), self.config, "DMDOX-318")
        self.assertEqual(result["window"], "DMDOX-318")
        self.assertEqual(result["pane"], "Pražské Benátky s.r.o.")

    def test_non_ticket_agent_and_shell_fallback(self):
        p = pane(agent="claude", terminal_title_stripped="Repair the deployment")
        self.assertEqual(resolve(p, self.config)["pane"], "Repair the deployment")
        self.assertEqual(resolve(p, self.config)["window"], "Pražské Benátky s.r.o.")
        self.assertEqual(resolve(pane(terminal_title_stripped="secret shell command"), self.config)["pane"], "Pražské Benátky s.r.o.")

    def test_generic_agent_status_uses_directory(self):
        for value in ("Claude Code", "Codex", "✳ Thinking…", "/tmp/test", "Dev"):
            self.assertEqual(resolve(pane(agent="claude", terminal_title_stripped=value), self.config)["pane"], "Pražské Benátky s.r.o.")

    def test_sanitize_controls_and_unicode(self):
        self.assertEqual(clean("\x1b[31mPražské\x1b[0m\nBenátky\u202e"), "Pražské Benátky")
        self.assertEqual(clean("\x1b]2;bad\x07Fine"), "Fine")
        self.assertEqual(shorten("ž" * 50, 24), "ž" * 23 + "…")

    def test_configuration_rejects_invalid_input(self):
        for values in ({"directory": "#0"}, {"directory": 2}, {"typo": True}, {"prefix_number": 1},
                       {"max_pane_length": 0}, {"poll_seconds": float("nan")}, {"ticket_titles": {"x": "y"}},
                       {"jira": [{"url": "https://jira.test"}]}, {"conversation_refresh_seconds": -1},
                       {"conversation_refresh_seconds": True}, {"conversation_refresh_seconds": 10}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                configuration(values)


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.config = configuration({})
        self.planner = Planner(self.config)
        self.snap = snapshot(pane(agent="codex", terminal_title_stripped="OPS-1425: New plugin | Dev"))

    def plan(self):
        contexts = {p["pane_id"]: resolve(p, self.config) for p in self.snap["panes"]}
        return self.planner.plan(self.snap, contexts)

    def commit(self, changes):
        for c in changes:
            obj = next(o for o in self.snap[c["kind"] + "s"] if o[c["kind"] + "_id"] == c["id"])
            obj["label"] = c["label"]
            self.planner.state["owned"][c["kind"] + ":" + c["id"]]["last"] = c["label"]

    def test_display_position_not_lifetime_tab_number(self):
        changes = self.plan()
        self.assertEqual(changes[-1]["label"], "1 · OPS-1425: New plugin")

    def test_prefix_disabled(self):
        self.config["prefix_number"] = False
        self.assertEqual(self.plan()[-1]["label"], "OPS-1425: New plugin")

    def test_unchanged_labels_do_not_write(self):
        self.commit(self.plan())
        self.assertEqual(self.plan(), [])

    def test_existing_manual_labels_preserved(self):
        self.snap["tabs"][0]["label"] = "Herdr plugins"
        self.snap["panes"][0]["label"] = "My custom pane"
        self.assertEqual(self.plan(), [{"kind": "tab", "id": "w1:t1", "before": "Herdr plugins", "label": "1 · Herdr plugins"}])
        self.commit(self.plan())
        self.assertEqual(self.plan(), [])

    def test_manual_name_is_preserved_when_prefix_changes(self):
        self.snap["tabs"][0]["label"] = "9 · Herdr plugins"
        self.commit(self.plan())
        self.assertEqual(self.snap["tabs"][0]["label"], "1 · Herdr plugins")
        self.snap["tabs"][0]["label"] = "My new name"
        self.commit(self.plan())
        self.assertEqual(self.snap["tabs"][0]["label"], "1 · My new name")
        self.assertTrue(self.planner.state["owned"]["tab:w1:t1"]["paused"])
        self.assertEqual(self.planner.state["owned"]["tab:w1:t1"]["original"], "My new name")

    def test_auto_title_becomes_manual_but_keeps_hotkey_prefix(self):
        self.commit(self.plan())
        self.snap["tabs"][0]["label"] = "Manual window"
        self.commit(self.plan())
        self.assertEqual(self.snap["tabs"][0]["label"], "1 · Manual window")
        self.snap["panes"][0]["title"] = "OPS-99: Changed task"
        self.assertFalse(any(c["kind"] == "tab" for c in self.plan()))
        self.snap["tabs"][0]["label"] = ""
        self.commit(self.plan())
        self.assertEqual(self.snap["tabs"][0]["label"], "1 · OPS-99: Changed task")

    def test_only_first_nine_tabs_get_hotkey_prefixes(self):
        self.snap = {"panes": [], "tabs": [
            {"tab_id": f"w1:t{i}", "workspace_id": "w1", "number": 100 + i,
             "label": f"{i} · Manual {i}"}
            for i in range(1, 12)
        ]}
        self.commit(self.plan())
        self.assertEqual([t["label"] for t in self.snap["tabs"]], [
            *(f"{i} · Manual {i}" for i in range(1, 10)), "Manual 10", "Manual 11",
        ])
        self.assertEqual(self.plan(), [])
        self.snap["tabs"].pop(0)
        self.commit(self.plan())
        self.assertEqual(self.snap["tabs"][8]["label"], "9 · Manual 10")
        self.assertEqual(self.snap["tabs"][9]["label"], "Manual 11")

    def test_reordering_manual_tabs_across_hotkey_boundary(self):
        self.snap = {"panes": [], "tabs": [
            {"tab_id": f"w1:t{i}", "workspace_id": "w1", "label": f"Name {i}"}
            for i in range(1, 11)
        ]}
        self.commit(self.plan())
        self.snap["tabs"].insert(0, self.snap["tabs"].pop())
        self.commit(self.plan())
        self.assertEqual(self.snap["tabs"][0]["label"], "1 · Name 10")
        self.assertEqual(self.snap["tabs"][9]["label"], "Name 9")

    def test_automatic_tabs_also_stop_numbering_at_ten(self):
        self.snap["tabs"] = [
            {"tab_id": f"w1:t{i}", "workspace_id": "w1", "label": str(i)}
            for i in range(1, 11)
        ]
        self.snap["panes"] = [pane(f"w1:p{i}", tab_id=f"w1:t{i}") for i in range(1, 11)]
        self.commit(self.plan())
        self.assertEqual(self.snap["tabs"][8]["label"], "9 · Pražské Benátky s.r.o.")
        self.assertEqual(self.snap["tabs"][9]["label"], "Pražské Benátky s.r.o.")

    def test_prefix_disabled_removes_decoration_without_truncating_manual_text(self):
        name = "Pražské Benátky " * 12
        self.snap["tabs"][0]["label"] = name
        self.commit(self.plan())
        self.assertEqual(self.snap["tabs"][0]["label"], "1 · " + name)
        self.config["prefix_number"] = False
        self.commit(self.plan())
        self.assertEqual(self.snap["tabs"][0]["label"], name)

    def test_duplicate_prefixes_are_normalized(self):
        self.snap["tabs"][0]["label"] = "3 · 7 · My tab"
        self.commit(self.plan())
        self.assertEqual(self.snap["tabs"][0]["label"], "1 · My tab")

    def test_manual_rename_pauses_until_cleared(self):
        self.commit(self.plan())
        self.snap["panes"][0]["label"] = "Leave this alone"
        self.snap["panes"][0]["title"] = "Fix another thing"
        self.assertFalse(any(c["kind"] == "pane" for c in self.plan()))
        self.snap["panes"][0]["label"] = ""
        self.assertTrue(any(c["kind"] == "pane" and c["label"] == "Fix another thing" for c in self.plan()))

    def test_pending_rename_survives_restart(self):
        changes = self.plan()
        for change in changes:
            self.planner.state["owned"][change["kind"] + ":" + change["id"]]["pending"] = change["label"]
            self.snap[change["kind"] + "s"][0]["label"] = change["label"]
        self.planner = Planner(self.config, copy.deepcopy(self.planner.state))
        self.assertEqual(self.plan(), [])
        self.assertFalse(any(v.get("paused") for v in self.planner.state["owned"].values()))

    def test_anchor_does_not_follow_focus(self):
        self.snap["panes"].append(pane("w1:p2", agent="claude", title="OPS-99: Another ticket", focused=True))
        self.commit(self.plan())
        self.snap["panes"].reverse()
        self.assertEqual(self.plan(), [])
        self.snap["panes"] = self.snap["panes"][:1]
        self.assertEqual(self.plan()[-1]["label"], "1 · OPS-99: Another ticket")

    def test_numbering_resets_per_workspace_and_moves_after_close(self):
        self.snap["tabs"].insert(0, {"tab_id": "w1:t0", "workspace_id": "w1", "number": 80, "label": "1"})
        self.snap["tabs"][1]["label"] = "2"
        self.snap["panes"].append(pane("w1:p0", tab_id="w1:t0"))
        self.snap["tabs"].append({"tab_id": "w2:t1", "workspace_id": "w2", "number": 90, "label": "1"})
        self.snap["panes"].append(pane("w2:p1", tab_id="w2:t1"))
        self.commit(self.plan())
        self.assertEqual(self.snap["tabs"][1]["label"], "2 · OPS-1425: New plugin")
        self.assertTrue(self.snap["tabs"][2]["label"].startswith("1 · "))
        self.snap["tabs"].pop(0)
        self.snap["panes"] = [p for p in self.snap["panes"] if p["pane_id"] != "w1:p0"]
        self.assertEqual(self.plan()[-1]["label"], "1 · OPS-1425: New plugin")

    def test_explicit_adoption_retains_original_for_restore(self):
        self.config["adopt_existing"] = True
        self.snap["tabs"][0]["label"] = "Old automatic title"
        self.assertTrue(self.plan())
        self.assertEqual(self.planner.state["owned"]["tab:w1:t1"]["original"], "Old automatic title")


if __name__ == "__main__":
    unittest.main()
