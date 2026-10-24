---
name: sweep
description: Sweep abandoned sessions in a directory or across the machine, applying filing rules and writing a watch-only sweep report. Safe for unattended runs.
---

# Sweep

Use the same procedure in Claude Code and Codex. Resolve the package root from
this skill's actual directory (`../..`); the shared command is
`scripts/lib/sweep.py`. This release supports watch-only mode: never run a record
write, a slot write, an ask command, an archive move, or worktree removal. The
only write is its sweep report. Never write a handoff for the sweep itself.

Take the requested directory or `all` as scope. If none is given, use the
working directory. Read the user checklist profile at `$CHECKLIST_USER_PROFILE`,
else `checklist/profile.md` under `$XDG_CONFIG_HOME` (default `~/.config`). Omit
`--user-profile` when absent. The existing profile binds `record_read`, the
record/knowledge/lessons slots, and the one ask command; do not invent bindings.

## Inspect and decide

Run the command, replacing the package path and scope:

```bash
python3 <package>/scripts/lib/sweep.py inspect --scope <directory-or-all>
```

Unattended runs use idle time alone with no open-session checks configured.
They never wait for input. Interactive runs first inspect the available session
surfaces read-only, then pass `--interactive --open-sessions <evidence-file>`
to both inspect and run. Evidence is JSON with `session_ids` (possibly empty)
and `method` (how the session surfaces were inspected). Never claim no open
sessions when inspection failed; report that blocker. No process is closed or
wrapped by the sweep. The report's open idle sessions have the choices `wrap it`,
`leave it`, `close it`; present these directly to the user after writing the
interactive report. Do not wait before completing the report.

Read the [checklist filing rules](../checklist/SKILL.md#5-write-and-validate-the-handoff--fixed)
and apply their ordered table to **every** item in an abandoned session, including
untyped items and schema-version-1 handoffs. Interpret the text and disposition;
do not classify solely by item type or keyword. An observation with no outcome
is R6; a concrete deliverable needing work is R5 only after a successful open
ticket search. Mentioning a ticket is insufficient for R2 unless it is open and
the item belongs on it. A proposed change to an approved spec is R3, not R7.
Knowledge for reuse is R4; information already saved in its named destination is
R1 only with evidence from a successful read. Anything a lesson/knowledge slot
would leave pending becomes an ask under R4 with `destination: "ask"`.

Use the profile's read-only `record_read` skill for R1 already-recorded checks,
R2 ticket status, and R5 matching searches. A failed, unbound, unsupported or
malformed read is unknown: supply `read_status: "failed"` and the error. Never
write as part of those reads. For multiple plausible matches, choose R3 with
choices naming the candidates; one matching open ticket becomes R2. Give R5
`priority` and `priority_reason`. Give R1/R6 a `reason`; only information-only R1
uses `information_only: true`. Give R3 and R4-to-ask concrete `choices`.

Prepare an array of decisions in a caller-owned temporary JSON file. Each row
copies `handoff` from the candidate's `path`, copies the item's `identity`
**unchanged**, and adds `rule`, `destination` when applicable, plus the fields
above. No position identifies an item. Example shape (replace every value):

```json
[{"handoff":"/example/state/source.json","identity":{"session_id":"example-session","produced_at":"2026-10-01T00:00:00Z","text":"Optional observation"},"rule":"R6","reason":"No stated outcome"}]
```

## Write the sweep report

```bash
python3 <package>/scripts/lib/sweep.py run --scope <directory-or-all> \
  --decisions <decisions-file> --user-profile <profile-file>
```

Pass the same interactive evidence when applicable. The command validates
choices through the checklist filing engine and checks current source identities;
a changed item requires a fresh inspection and decision. Omitted decisions stay
pending and keep their handoffs. A concurrent sweep exits with `already running`
and writes nothing. Other errors are reported explicitly; do not treat them as
an empty store. Read the report path printed on stdout. Report each missing
binding, failed read, parse error, retained handoff and proposed action. Its text
and embedded JSON describe the same run. A dry-run archive or worktree removal
is a proposal, never a completed operation. With no work it still writes a report.

Scheduling belongs to the user. See [the command contract and scheduling example](../../docs/sweep.md).
