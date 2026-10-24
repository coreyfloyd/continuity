# Sweep command

Sweep finds abandoned sessions and writes one watch-only sweep report. Invoke
`/continuity:sweep <directory-or-all>` in Claude Code, or ask Codex to use the
`sweep` skill with that scope. Both use the same skill and standard-library
command. A directory includes every handoff for its repository, across sibling
worktrees. Outside a repository, only that exact directory matches. `all`
includes the whole local handoff store, including legacy handoffs without a
subject (reported under `unknown directory`). Archives are not scanned.

A session is abandoned only when its newest handoff or transcript write is
strictly older than the idle threshold. Both Claude Code transcripts and Codex
active and archived rollouts count. A transcript store that cannot be inspected
prevents filing for that run and is reported. Malformed handoffs are reported
with their paths and reasons and left in place. No check executes a binding.

## Configuration

Add a `sweep` object to the existing continuity configuration file resolved by
`CONTINUITY_CONFIG`, else `$XDG_CONFIG_HOME/continuity/config.json` (default
`~/.config/continuity/config.json`). Other configuration is preserved.

```json
{
  "sweep": {
    "mode": "watch-only",
    "idle_days": 2,
    "open_session_checks": [],
    "new_ticket_limit": 10,
    "report_location": "~/sweep-reports",
    "archive_retention_days": null
  }
}
```

`mode` defaults to `watch-only`; this release refuses `act` without making
changes. `idle_days` is a non-negative finite number. `report_location` is a
directory; absent, reports go to `sweep-reports` beside the resolved handoff
store. A report location inside the handoff store is rejected. The handoff
store itself follows `HANDOFF_STATE_DIR`, then `handoff_dir`, then the existing
default. No handoff migration is performed.

`open_session_checks` reserves the later command-check integration and defaults
to none. Nonempty command lists currently stop the command before any sweep,
rather than silently ignoring protection. `new_ticket_limit` (a non-negative
integer, default 10) and `archive_retention_days` (default null, retention off)
are reserved for action execution. Watch-only creates no ticket and deletes
nothing, regardless of these values.

## Command and judgment contract

From the package root:

```bash
python3 scripts/lib/sweep.py inspect --scope all
python3 scripts/lib/sweep.py run --scope all --decisions /example/decisions.json
```

`inspect` prints candidates as JSON and writes nothing. Candidates include the
original handoff, directory, session id, latest activity, idle seconds,
abandoned status, and every item's identity. Identity consists of the producing
session, production time and text. Legacy items without provenance use their
source handoff's session and timestamp, falling back to the filename and file
write time. No item is identified by its array position.

Each inspected item has `identity_occurrences`, the number of times its identity
appears within that handoff. More than one occurrence is ambiguous, including
exact copies: fields outside the identity can affect filing, and the sweep
does not guess which copy a judgment meant. For a swept handoff, all copies
remain visible with the outcome `ambiguous identity`, their unchanged identity and source item, the
occurrence count, and a reason requiring manual judgment. No filing rule or
action is proposed for these copies, and their handoff is kept. Zero, one or
multiple supplied decisions for an ambiguous identity all produce that same
retained outcome per copy; none is applied. Discovery repeats this check at
run time. This check is local to a handoff: carrying the same identity into a
separate successor handoff is valid and does not create an ambiguity.

The sweep skill chooses filing rules using the checklist's ordered filing
rules, then supplies an array via `--decisions`. Each row names `handoff` (the
candidate's path), copies `identity` unchanged, and supplies the checklist
filing fields: `rule`, `destination`, `reason`, `choices`, `read_status`,
`information_only`, `priority`, `priority_reason`, and `error` as applicable.
The engine uses the checklist's filing validator and binding resolution,
without executing the bindings. Untyped legacy items use the same fields for
every rule. For R4, `destination: "lessons"` or `"knowledge"` supplies the agent's
classification to the validator using a temporary typed copy; the report keeps
the original item and its identity. R4 with `destination: "ask"` routes pending
slot work to an ask. `--user-profile` accepts the existing JSON or
Markdown checklist profile. Unbound destinations keep their handoffs and are
reported. R1/R2/R5 reads must succeed; failed or unsupported reads remain
`read failed`, never evidence that no ticket exists. Missing decisions stay
`pending judgment`. Stale and unknown decisions fail explicitly, as do multiple
decisions for an identity that occurs only once in its source handoff.
A direct run without decisions remains useful as an inventory and archives
no handoff carrying undecided items.

Unattended runs never request input. Interactive runs require `--interactive
--open-sessions /example/open.json` on both commands. This evidence object has
`session_ids` (an array) and `method` (a description of the read-only inspection).
An empty result is explicit. The report records that evidence and lists open
sessions with their idle time. The skill presents the choices `wrap it`,
`leave it`, `close it` directly for open idle sessions. This command neither
waits for those choices nor acts on them. An open or recently active session
also protects a worktree from any proposed removal.

`run` acquires an exclusive nonblocking lock on the handoff store's directory
inode before discovering candidates. It writes no lock file; when the store
is absent it locks the nearest existing ancestor conservatively. A concurrent
sweep over the same store exits 2 with `already running` and writes nothing.
Other command/configuration failures exit 1. Successful inspect/run exits 0;
a successful run prints its report path. Source parse failures appear inside
the report, so they do not prevent reporting the remaining sources. An item's
planning or validation failure also stays in the report: its outcome is `failed`,
with the source item, unchanged identity, chosen rule/destination and error. Its
handoff is kept, and the run continues with other items and handoffs. Exit 0 means
the report was written; read its outcomes to distinguish failures from proposals.

## Sweep report

Each run writes exactly one new Markdown file named by its run id, containing
text and an embedded JSON object for structured consumers. Parse the sole
`json` fence to read that same run. The object includes run id, host, start and
end times, scope, mode, open-session inspection results, parse errors, and
`directories`. Every directory has these arrays in order:

1. `asks_raised`: proposed asks, source session/directory/ticket and choices.
2. `open_sessions`: sessions protected by evidence, with idle seconds.
3. `new_tickets`: proposed new tickets, recommended priority and reason.
4. `comments_added`: proposed ticket comments.
5. `documents_changed`: proposed document edits.
6. `items_dropped`: proposed drops with the deciding rule and reason.
7. `worktrees_removed`: proposed worktree removals with branch, commit and checks.
8. `handoffs_archived`: proposed archives with source and destination.

`filing_outcomes` includes every item, including lessons/knowledge, pending
judgments, ambiguous identities, failed items, read failures and unbound
destinations. `worktrees_kept` and `handoffs_kept` explain retention. Every decided
item names its rule.
`dry run` means a proposal; nothing has been filed, dropped, archived, or
removed. An uncommitted-files ask names the session, directory and files and
always keeps its handoff. Proposed archives require every item to be fileable
or droppable and no unbound ask. Proposed worktree removals require a linked
checkout with no changed/untracked files and its branch contained in the
locally observed remote trunk; plain removal is explicitly unattempted.
The main checkout is never proposed for removal. Failed checks raise proposed
asks. Git remote refs are not fetched or changed by watch-only.

A run with no work still writes a report with `nothing_to_do: true` and
`Nothing to do.` This is distinct from a run with parse or discovery errors.

## External scheduling

The package ships no scheduler. For example, a user-owned cron entry can run a
local agent driver daily. Replace the paths with an installed package and your
existing unattended driver; the driver invokes the sweep skill and supplies
its read-only judgments:

```cron
0 3 * * * /absolute/path/to/agent-driver 'Use the sweep skill with scope all unattended'
```

For a deterministic inventory without an agent's filing judgments, schedule the
command directly instead:

```cron
0 3 * * * /usr/bin/python3 /absolute/path/to/continuity/scripts/lib/sweep.py run --scope all
```

The driver's environment must resolve the same configuration and local handoff
store as interactive use. No session, transcript, handoff, binding destination,
or worktree is modified by either watch-only route. Review real-directory
reports and their rule choices before enabling a future action-capable release.
