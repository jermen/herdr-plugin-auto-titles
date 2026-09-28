"""Derive short task labels from exact, local Claude/Codex conversations."""
import json
import os
import re
import time
from pathlib import Path

from titles import clean, description, shorten, task_words, ticket, title_key

MAX_SCAN = 4 * 1024 * 1024
MAX_HEADER = 64 * 1024
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,160}\Z")

# These turns steer execution of the existing task; they do not name a new one.
# Keep patterns anchored so a concrete request such as 'Fix deployment retries'
# or 'Create a merge request dashboard' is still a task in its own right.
FOLLOW_UP = re.compile(
    r"(?:yes|no|ok(?:ay)?|approved?|confirmed?|proceed|continue|go ahead|do (?:it|that)|"
    r"thanks?|thank you|done|sure|ano|ne|díky|pokračuj)(?:[ ,]+(?:please|thanks|do it|go ahead))?"
    r"|(?:any (?:updates?|progress)|(?:what(?:'s| is) (?:the )?)?status(?: update)?|"
    r"are (?:we|you) (?:done|finished)(?: yet)?|how is it going|what(?:'s| is) next)"
    r"|(?:one more thing|another thing|next step)"
    r"|(?:looks? good|sounds? good|continue with (?:it|that|this))"
    r"|(?:the )?(?:context|value|field|result|output) is still (?:empty|missing|wrong|unchanged)"
    r"|(?:fix|change|improve|check|test|deploy|merge|push|commit|do|try|use|implement)\s+"
    r"(?:it|that|this|them|those|the same|again|(?:this|that|the) (?:approach|option|solution))"
    r"(?:\s+(?:again|please|now|too|as well))?"
    r"|(?:run|rerun)(?: the)? (?:tests|checks|test suite|pipeline|ci)"
    r"(?: (?:and|then) (?:merge|push|commit)(?: it)?)?"
    r"|(?:commit|push|merge)(?: and (?:commit|push|merge))?(?: (?:it|this|the changes))?"
    r"|(?:create|open)(?: a| the)? (?:mr|pr|merge request|pull request)"
    r"|(?:close|resolve)(?: the| this)? (?:ticket|issue)"
    r"|(?:log|book)\s+\d+(?:\.\d+)?\s*(?:h|hours?|m|minutes?)(?:\s+.*)?"
    r"|(?:use|try|do it (?:with|in|using)) (?:docker|python|bash)"
    r"|(?:prepare|write|create)(?: a)?(?: python|bash|shell)? script(?: in /tmp)?",
    re.I,
)


def substantive(text):
    return (not FOLLOW_UP.fullmatch(text.strip(" .!?"))
            and len(task_words(text)) >= 2
            and not re.match(r"^(?:it|they)\b|^(?:this|that|those)(?:['’]s\b|\s+(?:now|still|is|are|works?)\b)", text, re.I)
            and not re.match(r"^I (?:cannot|can't|don't|do not) see (?:it|that|the (?:rest|text|output))\b", text, re.I)
            and not re.match(r"^on (?:another|this|the same) (?:machine|host)\b", text, re.I)
            and not re.fullmatch(r"(?:make|do) (?:it|this|that) .+", text, re.I))


def request_title(text):
    """Use the latest substantive request, not approvals or injected context."""
    if not isinstance(text, str):
        return ""
    text = text.strip()
    if text.startswith(("<", "# AGENTS.md", "# CLAUDE.md", "You are ", "[Request interrupted", "This session is being continued")):
        return ""
    # Quoted context and code are not the user's current request.
    text = re.sub(r"```[^\n]*\n[\s\S]*?(?:```|$)", "", text)
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith(">")]
    # Inspect sentences independently: 'Thanks. Please fix X' still contains a
    # task. Preserve line boundaries so a generic heading cannot swallow it.
    request_key = ""
    for line in lines:
        # Pasted JSON/logs are context, even when the user omits code fences.
        if (line.startswith(("{", "}", "[", "]", '"', "'", "/", "~/", "$ ", "Traceback (", "File \""))
                or re.match(r"^w\d+:p\d+\b", line)):
            continue
        for sentence in re.split(r"[?!.](?:\s|$)", line):
            text = clean(sentence).strip(" #*-:")
            text = re.sub(r"^(?:(?:yes|ok(?:ay)?|thanks)[,;:]\s*)", "", text, flags=re.I)
            text = re.sub(r"^(?:can|could|would|will) you\s+(?:please\s+)?|^please\s+|"
                          r"^I (?:want|need|would like) (?:you )?to\s+|^let['’]s\s+", "", text, flags=re.I)
            text = re.sub(r"\s*\(Recommended\)\s*$", "", text, flags=re.I)
            text = text.replace("`", "")
            key = ticket(text)
            detail = description(text, key) if key else text
            if key and not detail:
                request_key = key
            if key and re.match(r"^(?:resume|continue)\b", detail, re.I):
                return key  # Ticket summary is more useful than 'resume work'.
            if len(detail) < 5 or not substantive(detail):
                continue
            # Keep the key for title resolution, but remove conversational wrappers.
            title = detail[0].upper() + detail[1:]
            key = key or request_key
            return shorten(f"{key}: {title}" if key else title, 240)
    return request_key


def user_text(agent, row, sid):
    if not isinstance(row, dict):
        return ""
    if agent == "codex":
        payload = row.get("payload")
        if not isinstance(payload, dict):
            return ""
        if row.get("type") == "event_msg" and payload.get("type") == "user_message":
            return payload.get("message", "")
        if row.get("type") != "response_item" or payload.get("type") != "message" or payload.get("role") != "user":
            return ""
        content = payload.get("content")
        text_type = "input_text"
    else:
        if row.get("sessionId") != sid or row.get("type") != "user" or row.get("isSidechain") or row.get("isMeta") or row.get("isCompactSummary"):
            return ""
        message = row.get("message")
        if not isinstance(message, dict) or message.get("role") != "user":
            return ""
        content = message.get("content")
        text_type = "text"
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # Tool results, images and attached document bodies never become titles.
        return "\n".join(part["text"] for part in content if isinstance(part, dict)
                         and part.get("type") == text_type and isinstance(part.get("text"), str))
    return ""


def read_title(path, agent, sid):
    with path.open("rb") as stream:
        if agent == "codex":
            header = stream.readline(MAX_HEADER + 1)
            if len(header) > MAX_HEADER:
                return ""
            meta = json.loads(header)
            if not isinstance(meta, dict) or meta.get("type") != "session_meta" or not isinstance(meta.get("payload"), dict) or meta["payload"].get("id") != sid:
                return ""
        # Snapshot the size so a growing file cannot make this read unbounded.
        size = os.fstat(stream.fileno()).st_size
        start = max(0, size - MAX_SCAN)
        stream.seek(start)
        data = stream.read(size - start)
    if start:
        data = data.partition(b"\n")[2]  # Discard the first partial record.
    for line in reversed(data.split(b"\n")[:-1]):  # Ignore the incomplete final write.
        try:
            title = request_title(user_text(agent, json.loads(line), sid))
            if title:
                return title
        except (UnicodeError, ValueError, TypeError):
            continue
    return ""


class ConversationTitles:
    def __init__(self, roots=None, clock=time.monotonic):
        home = Path.home()
        self.roots = roots or {
            "codex": Path(os.environ.get("CODEX_HOME", home / ".codex")),
            "claude": Path(os.environ.get("CLAUDE_CONFIG_DIR", home / ".claude")),
        }
        self.clock = clock
        self.cache = {}

    def identity(self, pane):
        agent = pane.get("agent")
        binding = pane.get("agent_session") or {}
        sid = binding.get("value")
        if agent not in self.roots or binding.get("kind") != "id" or binding.get("agent", agent) != agent:
            return None
        if not isinstance(sid, str) or not SESSION_ID.fullmatch(sid):
            return None
        return agent, sid

    def locate(self, agent, sid):
        root = self.roots[agent]
        patterns = [f"sessions/*/*/*/*-{sid}.jsonl", f"archived_sessions/*-{sid}.jsonl"] if agent == "codex" else [f"projects/*/{sid}.jsonl"]
        for pattern in patterns:
            matches = list(root.glob(pattern))
            if len(matches) == 1 and matches[0].resolve().is_relative_to(root.resolve()):
                return matches[0]
            if len(matches) > 1:
                return None  # Never guess between ambiguous session files.
        return None

    def get(self, pane, interval):
        key = self.identity(pane)
        if not key or not interval:
            return ""
        now = self.clock()
        previous = self.cache.get(key)
        if previous and now - previous["checked"] < interval:
            return previous["title"]
        entry = {"checked": now, "title": previous["title"] if previous else "", "path": previous["path"] if previous else None}
        try:
            path = entry["path"] or self.locate(*key)
            entry["path"] = path
            if path:
                title = read_title(path, *key)
                if title:
                    # Cosmetic changes are not a new task.
                    if title_key(title) != title_key(entry["title"]):
                        entry["title"] = title
        except (OSError, ValueError, TypeError):
            entry["path"] = None
        self.cache[key] = entry
        return entry["title"]

    def prune(self, panes):
        active = {self.identity(pane) for pane in panes}
        self.cache = {k: v for k, v in self.cache.items() if k in active}
