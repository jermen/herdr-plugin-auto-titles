"""Title selection and ownership, independent of the Herdr transport."""
import os
import re
import subprocess
import time
from pathlib import PurePath

TICKET = re.compile(r"(?<![A-Za-z0-9])([A-Z][A-Z0-9]{1,19}-[0-9]+)(?![A-Za-z0-9])", re.I)
DEFAULTS = {
    "directory": "#2", "prefix_number": True, "max_window_length": 64,
    "max_pane_length": 80, "poll_seconds": 2, "adopt_existing": False,
    "ticket_titles": {}, "jira": [], "conversation_refresh_seconds": 180,
}


def clean(value):
    if not isinstance(value, str):
        return ""
    value = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", value)
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
    value = re.sub(r"[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]", " ", value)
    return " ".join(value.split())


def shorten(text, limit):
    text = clean(text)
    if len(text) <= limit:
        return text
    head = text[:limit - 1].rstrip()
    # Prefer complete words, but still bound long paths/identifiers without spaces.
    if not text[limit - 2].isspace() and not text[limit - 1].isspace() and " " in head:
        boundary = head.rfind(" ")
        if boundary >= limit // 2:
            head = head[:boundary]
    return head + "…"


def strip_tab_prefix(label):
    """The leading number-dot decoration is reserved for the tab hotkey."""
    return re.sub(r"^(?:[0-9]+[ \t]*·[ \t]*)+", "", label)


def configuration(raw):
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
        raise ValueError("unknown configuration key or invalid config object")
    c = DEFAULTS | raw
    if not isinstance(c["directory"], str) or not re.fullmatch(r"#[1-9][0-9]*", c["directory"]):
        raise ValueError("directory must be #1, #2, ... counting from the end")
    for name in ("prefix_number", "adopt_existing"):
        if not isinstance(c[name], bool):
            raise ValueError(name + " must be boolean")
    for name in ("max_window_length", "max_pane_length"):
        if type(c[name]) is not int or not 24 <= c[name] <= 240:
            raise ValueError(name + " must be an integer between 24 and 240")
    if type(c["poll_seconds"]) not in (int, float) or not 0.5 <= c["poll_seconds"] <= 60:
        raise ValueError("poll_seconds must be between 0.5 and 60")
    refresh = c["conversation_refresh_seconds"]
    if type(refresh) is not int or (refresh != 0 and not 30 <= refresh <= 3600):
        raise ValueError("conversation_refresh_seconds must be 0 (disabled) or 30 to 3600")
    if not isinstance(c["ticket_titles"], dict) or any(
        not isinstance(k, str) or not TICKET.fullmatch(k) or not isinstance(v, str)
        for k, v in c["ticket_titles"].items()
    ):
        raise ValueError("ticket_titles must map ticket IDs to titles")
    c["ticket_titles"] = {k.upper(): clean(v) for k, v in c["ticket_titles"].items()}
    if not isinstance(c["jira"], list):
        raise ValueError("jira must be a list")
    for site in c["jira"]:
        if not isinstance(site, dict) or set(site) != {"url", "projects", "email", "token_env"}:
            raise ValueError("each Jira site needs url, projects, email and token_env")
        if not re.fullmatch(r"https://[A-Za-z0-9.-]+(?::[0-9]+)?", site["url"]):
            raise ValueError("Jira URL must be an HTTPS origin without a path")
        if not isinstance(site["projects"], list) or not site["projects"] or any(
            not isinstance(p, str) or not re.fullmatch(r"[A-Z][A-Z0-9]{1,19}", p) for p in site["projects"]
        ):
            raise ValueError("Jira projects must be explicit uppercase project keys")
        if not isinstance(site["email"], str) or "@" not in site["email"]:
            raise ValueError("Jira email is required")
        if not isinstance(site["token_env"], str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", site["token_env"]):
            raise ValueError("token_env must name an environment variable")
    return c


def directory_title(cwd, selector):
    parts = [p for p in PurePath(cwd or "").parts if p not in ("/", ".")]
    if not parts:
        return "Terminal"
    depth = int(selector[1:])
    return clean(parts[-min(depth, len(parts))]) or "Terminal"


def topic(value, cwd=""):
    text = clean(value)
    text = re.sub(r"^(?:\[[^\]]*\]\s*)?(?:Action Required|Approval Required|Working|Thinking|Done)\s*\|\s*", "", text, flags=re.I)
    # Codex appends the working directory to its terminal title.
    suffix = " | " + PurePath(cwd).name if cwd else ""
    if suffix and text.endswith(suffix):
        text = text[:-len(suffix)]
    text = re.sub(r"^(?:[✳✻✽✢✶✦⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]\s*)+", "", text)
    if re.fullmatch(r"(?:claude(?: code)?|codex|opencode|gemini|kimi|bash|zsh|fish|working|thinking|idle|done)(?:\s*[.…]*)?", text, re.I):
        return ""
    if text.startswith(("/", "~/")) or text == PurePath(cwd).name:
        return ""
    return text


def ticket(value):
    match = TICKET.search(value or "")
    return match.group(1).upper() if match else ""


def description(value, key):
    text = value
    if key:
        # Remove the whole ticket lead-in, not just its ID inside a sentence.
        text = re.sub(
            r"^(?:please\s+)?(?:let['’]s\s+)?(?:work on|handle|do|complete|implement|solve|improve|fix)\s+"
            + re.escape(key) + r"\b\s*[:–—-]?\s*", "", text, flags=re.I,
        )
        text = re.sub(r"(?<![A-Za-z0-9])" + re.escape(key) + r"(?![A-Za-z0-9])", "", text, flags=re.I)
    return text.strip(" :-–—·()[]")


def task_words(text):
    """Small comparison vocabulary, not a generated summary or language model."""
    stop = set("a an the this that it its is are was were be been very often "
               "and or to of for in on with from by my our your you we i please "
               "can could would will should need want let's".split())
    words = re.findall(r"[^\W_]+", text.casefold(), re.UNICODE)
    return {word[:-1] if len(word) > 4 and word.endswith("s") else word
            for word in words if len(word) > 1 and word not in stop}


def title_key(text):
    # Preserve word order and negation: 'copy A to B' differs from 'copy B to A'.
    return clean(text).casefold().rstrip(".!?")


def reported_matches_request(reported, recent):
    """Use an agent's concise wording only when it describes the current request."""
    actions = set("add remove delete fix repair improve update upgrade migrate create "
                  "deploy enable disable investigate check explain copy move restore sync replace rename "
                  "increase decrease reduce raise lower restart stop start install uninstall".split())
    left, right = task_words(reported), task_words(recent)
    if left & actions and right & actions and not (left & right & actions):
        return False
    if right & actions and (left & {"not", "never", "without"}) != (right & {"not", "never", "without"}):
        return False
    left, right = left - actions, right - actions
    if task_words(recent) & actions and left != right:
        return False  # Do not lose a new target/qualifier from an explicit task.
    # One shared word is too weak: 'reconnect tests' is not 'reconnect handling'.
    shared = left & right

    def order(text):
        return [word for token in re.findall(r"[^\W_]+", text, re.UNICODE)
                for word in task_words(token) if word in shared]

    return (len(shared) >= 2 and len(shared) / max(1, len(left)) >= 0.6
            and order(reported) == order(recent))


class Branches:
    def __init__(self):
        self.cache = {}

    def get(self, cwd):
        if not cwd:
            return ""
        now = time.monotonic()
        old = self.cache.get(cwd)
        if old and now - old[0] < 15:
            return old[1]
        value = ""
        try:
            result = subprocess.run(
                ["git", "-C", cwd, "symbolic-ref", "--quiet", "--short", "HEAD"],
                text=True, capture_output=True, timeout=1,
                env=os.environ | {"GIT_OPTIONAL_LOCKS": "0"},
            )
            if result.returncode == 0:
                value = clean(result.stdout)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if len(self.cache) > 256:
            self.cache.clear()
        self.cache[cwd] = (now, value)
        return value


def resolve(pane, config, branch="", summary=lambda key: ""):
    cwd = pane.get("effective_cwd") or pane.get("cwd") or pane.get("foreground_cwd") or ""
    tokens = pane.get("tokens") or {}
    task = topic(tokens.get("task_title"), cwd) or topic(pane.get("title"), cwd)
    task = task or (topic(pane.get("terminal_title_stripped"), cwd) if pane.get("agent") else "")
    key = ticket(tokens.get("ticket_id")) or ticket(task) or ticket(branch)
    short = config["ticket_titles"].get(key) or clean(tokens.get("ticket_summary")) or (summary(key) if key else "")
    detail = description(task, key) if key else task
    if key and not detail:
        # A descriptive branch can help when the only task context is its ticket.
        detail = re.sub(r"[-_/]+", " ", description(branch.rsplit("/", 1)[-1], key))
    fallback = directory_title(cwd, config["directory"])
    pane_title = detail or short or fallback
    recent = topic(pane.get("conversation_title"), cwd)
    if recent and not topic(tokens.get("task_title"), cwd):
        recent_key = ticket(recent)
        recent_detail = description(recent, recent_key) if recent_key else recent
        # A bare 'Do TICKET-123' contains no task; keep the normal summary fallback.
        if recent_detail:
            pane_title = detail if detail and reported_matches_request(detail, recent_detail) else recent_detail
        elif recent_key:
            pane_title = (short or pane_title) if recent_key == key else (
                config["ticket_titles"].get(recent_key) or summary(recent_key) or recent_key)
    window = (key + (": " + (short or detail) if short or detail else "")) if key else fallback
    return {"pane": shorten(pane_title, config["max_pane_length"]), "window": window, "ticket": key}


class Planner:
    def __init__(self, config, state=None):
        self.config = config
        self.state = state if state is not None else {"owned": {}, "anchors": {}}
        self.state.setdefault("owned", {})
        self.state.setdefault("anchors", {})

    def owned(self, kind, obj, position=None):
        ident = obj[kind + "_id"]
        key = kind + ":" + ident
        current = obj.get("label") or ""
        record = self.state["owned"].get(key)
        unnamed = not current or (kind == "tab" and current == str(position))
        if record:
            external = current not in (record.get("last"), record.get("pending"))
            if unnamed:
                record["paused"] = False
            elif external:
                record["paused"] = True
            if kind == "tab" and external:
                # We still decorate manually renamed tabs. Restore must retain
                # that new manual name instead of restoring an older auto title.
                record["original"] = current
            return not record.get("paused", False)
        if unnamed or self.config["adopt_existing"]:
            self.state["owned"][key] = {"original": current, "last": current}
            return True
        return False

    def plan(self, snapshot, contexts):
        changes = []
        live = set()
        groups = {}
        for pane in snapshot.get("panes", []):
            pid = pane["pane_id"]
            live.add("pane:" + pid)
            groups.setdefault(pane["tab_id"], []).append(pane)
            if self.owned("pane", pane):
                value = contexts[pid]["pane"]
                if value != (pane.get("label") or ""):
                    changes.append({"kind": "pane", "id": pid, "before": pane.get("label") or "", "label": value})
        positions = {}
        for tab in snapshot.get("tabs", []):
            tid, wid = tab["tab_id"], tab["workspace_id"]
            live.add("tab:" + tid)
            positions[wid] = positions.get(wid, 0) + 1
            position = positions[wid]
            current = tab.get("label") or ""
            automatic = self.owned("tab", tab, position)
            self.state["owned"].setdefault("tab:" + tid, {
                "original": current, "last": current, "paused": True,
            })
            panes = groups.get(tid, [])
            prefix = f"{position} · " if self.config["prefix_number"] and position <= 9 else ""
            if automatic and panes:
                # Keep the same ticket-bearing pane through focus/status changes.
                candidates = [p for p in panes if contexts[p["pane_id"]]["ticket"]] or [p for p in panes if p.get("agent")] or panes
                anchor = self.state["anchors"].get(tid)
                chosen = next((p for p in candidates if p["pane_id"] == anchor), candidates[0])
                self.state["anchors"][tid] = chosen["pane_id"]
                value = shorten(prefix + contexts[chosen["pane_id"]]["window"], self.config["max_window_length"])
            else:
                # Prefix ownership is independent of automatic title ownership.
                # Preserve manual text verbatim, including long names.
                value = prefix + strip_tab_prefix(current)
            if value != current:
                changes.append({"kind": "tab", "id": tid, "before": current, "label": value})
        self.state["owned"] = {k: v for k, v in self.state["owned"].items() if k in live}
        self.state["anchors"] = {k: v for k, v in self.state["anchors"].items() if "tab:" + k in live}
        return changes
