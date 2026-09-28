#!/usr/bin/env python3
"""Automatic Herdr window and pane titles. Python 3.10+, standard library only."""
import argparse
import base64
import fcntl
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from titles import Branches, Planner, clean, configuration, directory_title, resolve, title_key, topic
from conversation import ConversationTitles

PLUGIN = "jermen.auto-titles"
MAX_REPLY = 8 * 1024 * 1024


def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def atomic(path, value):
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w") as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def call(endpoint, method, params=None):
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(4)
        sock.connect(endpoint)
        sock.sendall((json.dumps({"id": PLUGIN, "method": method, "params": params or {}}) + "\n").encode())
        with sock.makefile("rb") as stream:
            line = stream.readline(MAX_REPLY + 1)
    if len(line) > MAX_REPLY or not line.endswith(b"\n"):
        raise ValueError("invalid Herdr response")
    reply = json.loads(line)
    if reply.get("error"):
        raise ValueError("Herdr request failed: " + str(reply["error"].get("code", "unknown")))
    return reply["result"]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Tickets:
    """Optional, read-only Jira summaries. Tokens stay in the environment."""
    def __init__(self, config):
        self.config = config
        self.cache = {}
        self.budget = 1
        self.opener = urllib.request.build_opener(NoRedirect)

    def get(self, key):
        site = next((s for s in self.config["jira"] if key.split("-")[0] in s["projects"]), None)
        if not site:
            return ""
        cache_key = (site["url"], site["email"], site["token_env"], key)
        previous = self.cache.get(cache_key, (0, ""))
        if previous[0] > time.monotonic() or self.budget == 0:
            return previous[1]
        token = os.environ.get(site["token_env"])
        if not token:
            return ""
        self.budget -= 1  # At most one network lookup per poll.
        value = previous[1]
        ttl = 300
        try:
            auth = base64.b64encode((site["email"] + ":" + token).encode()).decode()
            request = urllib.request.Request(site["url"] + "/rest/api/3/issue/" + key + "?fields=summary",
                                             headers={"Authorization": "Basic " + auth, "Accept": "application/json"})
            with self.opener.open(request, timeout=3) as response:
                data = response.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise ValueError("Jira response too large")
            value = clean(json.loads(data).get("fields", {}).get("summary"))
            ttl = 3600
        except (OSError, ValueError, KeyError, TypeError):
            pass  # Keep the previous summary, never print authenticated responses.
        if len(self.cache) > 512:
            self.cache.clear()
        self.cache[cache_key] = (time.monotonic() + ttl, value)
        return value


class Contexts:
    def __init__(self, endpoint, config, clock=time.monotonic):
        self.endpoint = endpoint
        self.config = config
        self.branches = Branches()
        self.tickets = Tickets(config)
        self.processes = {}
        self.conversations = ConversationTitles(clock=clock)
        self.pane_titles = {}
        self.clock = clock

    def read(self, snapshot):
        self.tickets.config = self.config
        self.tickets.budget = 1
        result = {}
        now = self.clock()
        for pane in snapshot.get("panes", []):
            pid = pane["pane_id"]
            # Herdr's foreground_cwd can belong to an MCP child, so resolve the
            # foreground process group leader instead, with a short-lived cache.
            identity = (pane.get("terminal_id"), json.dumps(pane.get("agent_session")), pane.get("cwd"))
            cached = self.processes.get(pid)
            if cached is None or cached[0] != identity or now - cached[1] > 15:
                cwd = ""
                try:
                    data = call(self.endpoint, "pane.process_info", {"pane_id": pid})
                    info = data.get("process_info", data)
                    leader = info.get("foreground_process_group_id")
                    cwd = next((p.get("cwd") for p in info.get("foreground_processes", []) if leader and p.get("pid") == leader), "")
                except (OSError, ValueError):
                    pass
                cached = self.processes[pid] = (identity, now, cwd)
            cwd = cached[2] or pane.get("cwd") or pane.get("foreground_cwd") or ""
            recent = self.conversations.get(pane, self.config["conversation_refresh_seconds"])
            resolved = resolve(pane | {"effective_cwd": cwd, "conversation_title": recent}, self.config, self.branches.get(cwd), self.tickets.get)
            # Apply the same cadence to reported titles as transcript titles.
            # Windows and explicit task_title overrides continue updating promptly.
            title_identity = (identity, pane.get("agent"), self.config["directory"], self.config["max_pane_length"])
            old = self.pane_titles.get(pid)
            explicit = topic((pane.get("tokens") or {}).get("task_title"), cwd)
            interval = self.config["conversation_refresh_seconds"]
            if (old and old[0] == title_identity and now - old[1] < interval
                    and not explicit and not old[3]
                    and old[2] != directory_title(cwd, self.config["directory"])):
                resolved["pane"] = old[2]
            else:
                if (old and old[0] == title_identity and not explicit and not old[3]
                        and title_key(old[2]) == title_key(resolved["pane"])):
                    resolved["pane"] = old[2]
                self.pane_titles[pid] = (title_identity, now, resolved["pane"], bool(explicit))
            result[pid] = resolved
        self.processes = {k: v for k, v in self.processes.items() if k in result}
        self.conversations.prune(snapshot.get("panes", []))
        self.pane_titles = {k: v for k, v in self.pane_titles.items() if k in result}
        return result


def socket_identity(endpoint):
    info = os.stat(endpoint)
    return [info.st_dev, info.st_ino]


def load_state(path, endpoint):
    state = read_json(path, {})
    identity = socket_identity(endpoint)
    if state.get("server") != identity:
        state = {"server": identity}
    return state


def apply(endpoint, planner, changes, state_path):
    for change in changes:
        kind, ident = change["kind"], change["id"]
        # Re-read immediately before writing to catch a manual edit during planning.
        current = call(endpoint, kind + ".get", {kind + "_id": ident})[kind]
        if (current.get("label") or "") != change["before"]:
            continue
        record = planner.state["owned"][kind + ":" + ident]
        record["pending"] = change["label"]
        atomic(state_path, planner.state)  # A timed-out request may still apply.
        call(endpoint, kind + ".rename", {kind + "_id": ident, "label": change["label"]})
        record["last"] = change["label"]
        record.pop("pending", None)
        atomic(state_path, planner.state)


def restore(endpoint, state_path):
    state = load_state(state_path, endpoint)
    snapshot = call(endpoint, "session.snapshot")["snapshot"]
    for kind in ("pane", "tab"):
        for obj in snapshot[kind + "s"]:
            ident = obj[kind + "_id"]
            key = kind + ":" + ident
            saved = state.get("owned", {}).get(key)
            if not saved:
                continue
            current = call(endpoint, kind + ".get", {kind + "_id": ident})[kind]
            if (current.get("label") or "") in (saved.get("last"), saved.get("pending")):
                call(endpoint, kind + ".rename", {kind + "_id": ident, "label": saved["original"]})
            del state["owned"][key]
            atomic(state_path, state)


def settings(args):
    endpoint = args.socket or os.environ.get("HERDR_SOCKET_PATH")
    if not endpoint:
        raise ValueError("HERDR_SOCKET_PATH or --socket is required")
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    herdr = Path(os.environ.get("HERDR_CONFIG_PATH", config_home / "herdr/config.toml")).parent
    config_dir = Path(os.environ.get("HERDR_PLUGIN_CONFIG_DIR", herdr / "plugins/config" / PLUGIN))
    config_path = Path(args.config) if args.config else config_dir / "config.json"
    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    root = Path(os.environ.get("HERDR_PLUGIN_STATE_DIR", state_home / "herdr-auto-titles"))
    state = root / hashlib.sha256(os.path.abspath(endpoint).encode()).hexdigest()[:16]
    return endpoint, config_path, state


def locked(path):
    lock = path.open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return None
    return lock


def watch(endpoint, config_path, state):
    lock = locked(state / "watch.lock")
    if lock is None:
        return
    running = True

    def stop(_signum, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    state_path = state / "ownership.json"
    config = configuration(read_json(config_path, {}))
    planner = Planner(config, load_state(state_path, endpoint))
    contexts = Contexts(endpoint, config)
    identity = socket_identity(endpoint)
    try:
        while running and not (state / "stop").exists():
            start = time.monotonic()
            try:
                if socket_identity(endpoint) != identity:
                    break
                config = configuration(read_json(config_path, {}))
                planner.config = contexts.config = config
                snapshot = call(endpoint, "session.snapshot")["snapshot"]
                changes = planner.plan(snapshot, contexts.read(snapshot))
                apply(endpoint, planner, changes, state_path)
                atomic(state_path, planner.state)
                atomic(state / "health.json", {"pid": os.getpid(), "updated_at": time.time(), "server": identity,
                                               "panes": len(snapshot["panes"]), "changes": len(changes)})
            except FileNotFoundError:
                break
            except (OSError, ValueError, KeyError, TypeError):
                print("auto-titles: refresh failed", file=sys.stderr, flush=True)
            deadline = start + config["poll_seconds"]
            while running and not (state / "stop").exists() and time.monotonic() < deadline:
                time.sleep(0.1)
    finally:
        lock.close()
        (state / "health.json").unlink(missing_ok=True)


def stop(state):
    lock = locked(state / "watch.lock")
    if lock:
        lock.close()
        return
    (state / "stop").touch()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        lock = locked(state / "watch.lock")
        if lock:
            lock.close()
            return
        time.sleep(0.1)
    raise ValueError("watcher did not stop")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preview", "start", "watch", "stop", "restore", "status"))
    parser.add_argument("--socket")
    parser.add_argument("--config")
    args = parser.parse_args()
    endpoint, config_path, state = settings(args)
    config = configuration(read_json(config_path, {}))
    if args.command == "preview":
        snapshot = call(endpoint, "session.snapshot")["snapshot"]
        contexts = Contexts(endpoint, config).read(snapshot)
        planner = Planner(config, load_state(state / "ownership.json", endpoint))
        print(json.dumps({"titles": contexts, "changes": planner.plan(snapshot, contexts)}, ensure_ascii=False, indent=2))
        return
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.command == "watch":
        watch(endpoint, config_path, state)
    elif args.command == "status":
        health = read_json(state / "health.json", {})
        lock = locked(state / "watch.lock")
        health["healthy"] = lock is None and health.get("server") == socket_identity(endpoint) and time.time() - health.get("updated_at", 0) < 90
        if lock:
            lock.close()
        print(json.dumps(health))
    elif args.command in ("stop", "restore"):
        stop(state)
        if args.command == "restore":
            restore(endpoint, state / "ownership.json")
    else:
        lock = locked(state / "watch.lock")
        if lock is None:
            return
        (state / "stop").unlink(missing_ok=True)
        lock.close()
        command = [sys.executable, str(Path(__file__).resolve()), "watch", "--socket", endpoint, "--config", str(config_path.resolve())]
        with (state / "watch.log").open("a") as output:
            subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True, close_fds=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print("auto-titles: " + type(exc).__name__ + ": " + str(exc), file=sys.stderr)
        sys.exit(1)
