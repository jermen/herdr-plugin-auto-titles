# Automatic Titles for Herdr

A personal Herdr plugin that names windows (Herdr tabs) by ticket and panes by
the agent's task. Python 3.10 or newer; no packages, model calls, agent hooks, or
external plugins required. Linux and macOS, Herdr 0.9.1 or newer.

For `OPS-1425: New plugin`, the window becomes **2 · OPS-1425: New plugin**
and its pane becomes **New plugin**. A pane reporting `Fix reconnect race`
can keep that specific task while its window retains the ticket summary.

## Install

On the machine running the Herdr server:

```sh
herdr plugin install jermen/herdr-plugin-auto-titles --yes
herdr plugin action invoke preview --plugin jermen.auto-titles
herdr plugin action invoke start --plugin jermen.auto-titles
herdr plugin action invoke status --plugin jermen.auto-titles
```

Herdr starts the watcher by itself on later server starts. Reinstalling with
`herdr plugin install` updates the checkout in place. For development, link a
clone instead: `herdr plugin uninstall jermen.auto-titles`, then from the clone
run `python3 -B auto_titles.py preview` and `herdr plugin link "$PWD"`, followed
by the `start` action.

Disable any other plugin that renames tabs or panes before starting this one.
It works alongside `jermen.status-indicator`, which displays pane labels without
owning them. This plugin does not change Herdr's sidebar, terminal output, agent
settings, or workspace names. A linked development clone must stay on disk.

## Configuration

Copy `config.example.json` to `config.json` in the directory printed by:

```sh
herdr plugin config-dir jermen.auto-titles
```

Configuration reloads each poll. An absent file uses the defaults below; an
invalid file pauses updates and logs an error until corrected.

| Setting | Default | Meaning |
| --- | --- | --- |
| `directory` | `"#2"` | Component counted from the end: `#1` is the current directory, `#2` its parent. |
| `prefix_number` | `true` | Prefix tabs 1–9 in each workspace with their hotkey number and ` · `, including manually named tabs. Tabs 10+ have no prefix. |
| `max_window_length` | `64` | Maximum Unicode characters for automatic window names, including the prefix and ellipsis. Manual names are not truncated. |
| `max_pane_length` | `80` | Maximum Unicode characters including the ellipsis. |
| `poll_seconds` | `2` | Snapshot interval, from 0.5 to 60 seconds. |
| `conversation_refresh_seconds` | `180` | Refresh conversation and reported pane task titles every three minutes; `0` disables transcript reads and the pane-title cooldown, otherwise 30–3600 seconds. |
| `adopt_existing` | `false` | Also take ownership of existing nonempty labels; see manual names below. |
| `ticket_titles` | `{}` | Optional local map of ticket IDs to short summaries. |
| `jira` | `[]` | Optional explicit Jira sites for read-only summary lookups. |

For `/a/b/a/c/Pražské Benátky s.r.o./Dev`, `#2` produces
`Pražské Benátky s.r.o.`. A selector deeper than the path uses its first
component; an unknown directory becomes `Terminal`.

### Where titles come from

1. Pane metadata token `task_title`, then Herdr's reported `title`, then the
   agent's `terminal_title_stripped`. Shell command titles and generic agent
   status strings are ignored. Codex's ` | <directory>` suffix is removed.
2. Ticket: metadata token `ticket_id`, then the task title, then the current
   Git branch. Keys are normalized to uppercase.
3. Ticket summary: `ticket_titles`, metadata token `ticket_summary`, then an
   optional Jira lookup. Without a summary, use the description accompanying
   the ticket in the task title or branch.
4. Pane: the specific task description, then the ticket summary, then the
   configured directory. Window: ticket plus available description; if there
   is no ticket, the configured directory.

For Claude and Codex panes with an exact session ID, the plugin reads the local
conversation at startup and every three minutes. The most recent substantive
user request supplies the task. Generic openings, ticket lead-ins and polite
prefixes are removed; a concise reported title is preferred when its subject
matches that request. For example, a request complaining that pane names are
not descriptive can retain the reported **Improve pane name descriptions**.
Conflicting actions or reversed source/destination wording use the new request.

Approvals, status questions, generic execution follow-ups (such as **Run tests**,
**Merge it** or **Prepare a Python script**), pasted JSON/logs, tool results,
assistant replies, quoted context, and injected agent rules do not replace the
task. A concrete new request such as **Write a Python script to export Jira
worklogs** does. Ticket-only requests and requests to resume a ticket use its
available summary. These are conservative text heuristics, primarily for English
follow-ups; they do not generate semantic summaries of arbitrary conversations.

Reported pane titles share the three-minute cooldown, so they no longer change
on every two-second poll. Changes only in capitalization or trailing punctuation
keep the existing wording. A new session, replacing a directory fallback, or an
explicit `task_title` metadata override can update immediately. Windows retain
their ticket context and update every poll. Codex's `Action Required` status
banner is removed from reported titles. Long automatic titles truncate at a word
boundary when possible.

This is local text extraction, with no LLM calls or transcript uploads. Only the
file bound to the pane's session ID is read, at most its last 4 MiB per refresh;
the title is cached in memory between refreshes. Paths respect `CODEX_HOME` and
`CLAUDE_CONFIG_DIR`. Unsupported agents or unavailable transcripts use reported
titles. If a long tool response pushes the last request out of the read limit,
the previous conversation title is retained. A changed session ID clears that
association immediately. If the only available task is `Do DMDOX-318`, configure
Jira or `ticket_titles` to get its summary; otherwise the directory fallback is
used. Manual labels remain protected.

In a multi-pane window, the first ticket-bearing pane supplies its title. The
selection remains stable across focus/state changes, and changes when that pane
closes or loses its ticket context. Without a ticket, an agent pane takes
precedence over a shell. Each pane still gets its own task title.

The directory comes from the foreground process group leader when available.
This avoids accidentally naming a pane after an MCP subprocess's directory.
The shell directory and foreground directory are fallbacks. Process and Git
branch reads are cached for 15 seconds.

### Optional Jira summaries

Example configuration (replace the site and email):

```json
{
  "jira": [
    {
      "url": "https://example.atlassian.net",
      "projects": ["OPS", "DMDOX"],
      "email": "you@example.com",
      "token_env": "JIRA_API_TOKEN"
    }
  ]
}
```

The named token must be available to the **Herdr server/plugin process**.
Setting it in a different shell does not change the running server's environment.
No token is stored in this config or in plugin state. Requests only read the
summary of a detected ticket in an explicitly configured project. Redirects are
not followed. Successful results are cached in memory for an hour, failures for
five minutes; at most one request is made per poll. Cached summaries survive a
temporary lookup failure. Without credentials or network access, local context
and directory fallbacks continue working. Several ticket-only panes can take
several polls to acquire their summaries after startup.

For an offline alternative:

```json
{"ticket_titles": {"OPS-1425": "New plugin"}}
```

### Manual names and rollback

Existing nonempty title text is preserved by default, including names left by a
previous auto-title plugin. Tab hotkey prefixes are managed separately from the
title text: tabs 1–9 get `1 · ` through `9 · ` even after a manual rename, while
tabs 10 and higher get no prefix. Prefixes update when tabs move or close, reset
in each workspace, and replace any existing leading `number · ` decoration.
The leading decoration is reserved for hotkey numbering; manual text following
it is preserved verbatim. Pane labels do not receive numeric prefixes.

Clear a pane/window label to opt its title text into automatic naming. A tab's
default numeric position is also considered unnamed.

Setting `adopt_existing` to `true` explicitly opts all existing labels into
management. It does not bypass a later manual rename: any edit differing from
the plugin's last or pending write pauses automatic title text until the label
is cleared. Tab hotkey prefixes continue updating while title text is paused.
Ownership and original labels persist across plugin restarts and are scoped to
the server socket identity, avoiding reuse after a server restart.

```sh
# Stop changing labels, keeping current titles:
herdr plugin disable jermen.auto-titles
herdr plugin action invoke stop --plugin jermen.auto-titles

# Or stop and restore labels still owned by the plugin:
herdr plugin action invoke restore --plugin jermen.auto-titles
```

`restore` retains subsequent manual edits. Disable the plugin before restoring
so startup/event hooks cannot launch it again. The watcher uses an exclusive
file lock; duplicate startup/event hooks do not create duplicate writers. It
exits when the server socket disappears or is replaced.

`preview` reads current titles and prints proposed changes without changing
labels or writing state. `status` reports watcher health. Herdr actions pass the
correct config/state directories, so prefer actions for installed operation;
standalone commands also accept `--socket` and `--config` for isolated tests.

## Verify

```sh
python3 -B -m unittest discover -s tests -v
```

Tests cover ticket/task separation, Unicode paths, numbering, multi-pane
selection, manual edits, pending writes, restore, server identity, Jira caching,
conversation and reported-title refresh timing, session isolation, follow-up and
pasted-output filtering, concise title selection, meaningful topic changes,
and a real watcher lifecycle against an isolated Unix-socket fixture. CI uses
Python 3.12 and validates the plugin manifest.

The requested reference was
[kryptamine/herdr-auto-title](https://github.com/kryptamine/herdr-auto-title).
This is an independent implementation of the required behavior using Herdr's
socket API.
