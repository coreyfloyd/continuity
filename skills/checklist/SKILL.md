---
name: checklist
description: "End-of-task wrap-up for any session: run the five-step spine with user bindings and additive repository steps. Use when finishing a unit of work, or when the user says 'checklist', 'wrap up', or 'wrap up session'."
---

# Checklist — end-of-task wrap-up

Run one fixed spine: reconcile the record, capture knowledge, fold in lessons,
persist, then write and validate the handoff. The first four steps are slots.
Profiles bind them and add steps; the handoff is not a slot, and only an
`own-worktree` step may follow it.
Run the named binding skill as the step, preserving its own approval requirements.

## Resolve profiles before running the spine

Read the **user profile**, which applies to every session. It is
`$CHECKLIST_USER_PROFILE` when that is set, otherwise `checklist/profile.md` under
`$XDG_CONFIG_HOME` (default `~/.config`). Claude Code and Codex use the same
profile and composer. When no user profile file exists, run with none: that is a
configured-nothing user. The user profile may name further profile files to pass
as repository profiles for some surfaces; follow it.

Then identify every repository this session touched. The candidate detector is
this package's `scripts/checklist-surfaces.sh` (resolve the path from this
skill's real directory: `../../scripts/checklist-surfaces.sh`):

```bash
bash <checklist-surfaces.sh>
```

It lists the session's own repository plus the standing surfaces named by
`checklist_surfaces` in the continuity configuration (none by default).

Treat output as candidates: exclude another session's changes, and include your
own work even if already committed and pushed. For a linked worktree, read its
profile first if it differs from the main checkout's. A repository profile at
`.claude/checklist-profile.md` is additive. Read each complete file, including
notes that qualify its steps.

Profiles with a `checklist-profile` JSON fence are composed by the standard-library
module `scripts/lib/checklist.py`. The fence declares `name`, optional `bindings`,
and optional `steps`. A binding has a `command` and a list of item types it
`consumes`; a step has a unique `id`, an `after` anchor, and `instructions` (or
a `section` naming a level-two heading in the same Markdown file).

Run the composer with the user profile first and repository profiles in resolved
order. Paths below are placeholders to replace with the actual resolved files:

```bash
python3 <checklist-module> plan --user-profile <user-profile> \
  --repository-profile <repository-profile> --handoff <resumed-handoff> \
  --session-id <current-session-id>
```

Repeat `--repository-profile` for each additional file. Omit `--handoff` when no
handoff was resumed. `plan` writes a start marker row to the current session's
ledger; `--session-id` defaults to the Claude Code session in the environment,
so pass it in Codex. No profile is also a valid plan: omit the profile arguments, and the plan is the
five spine steps ending with the handoff. Record, knowledge, and persist are
unbound in the spine; lessons defaults to `/harness-improve`, consuming `lesson`
items. An unbound slot's `carries` lists the item types it leaves in the handoff.

Added steps may run `before-spine` or after `record`, `knowledge`, `lessons`, or
`persist`. At each anchor they run in profile order, then declaration order.
The composer rejects removal, skipping, replacement of a spine step, duplicate
step IDs, rebinding by repository profiles, and any step after `handoff` but
`own-worktree`; each error names the step. Report its error and stop; do not silently drop a
malformed profile.

Existing repository profiles without a fence still apply: read their `Slot:`
declarations and merge their instructions at the corresponding anchors using
the same additive rule. Reject attempts to remove/skip any spine step or add
after handoff, except `own-worktree`. A legacy `after-persist` declaration means after `persist`.
Do not silently omit a repository's existing steps during this transition.

## Who runs it, and when

Only an interactive session runs the checklist. Factory sessions and subagents
never run it and never write a handoff. The module refuses a headless session
(`a factory session never runs the checklist or writes a handoff`), and the
`checklist-actor-guard` PreToolUse hook, in both tools, denies a subagent or
factory session the module's operations and the checkpoint writers. A subagent
that reaches this skill stops and reports to its parent instead.

An interactive session its user launched and left to execute approved work (unattended
mode: a one-ticket scope or a slate) wraps itself, on the slate's cadence. The package
knows unattended mode only through the optional `unattended` section of the continuity
configuration (see `INTERFACE.md`); with none, every session is attended and this section
does not apply.

- **After each ticket** in the slate (or the scope's work landing), checkpoint: write
  the handoff with every unrecorded session item through `resume-checkpoint`'s script,
  as in step 5. Do not run the checklist;
  it would clean up worktrees and agents the slate still uses.
- **After the whole slate**, once its completion command (the configured
  `unattended.completion_command`) succeeds, run this full checklist once with
  `--unattended` on `plan`, `report`, and `wrap-check`.

`python3 <checklist-module> wrap-cadence` reads a hook payload on stdin and
prints which applies now: `none`, `checkpoint`, `unattended-checklist`, or
`checklist`. The completion command returns the session to attended mode, so the checklist
nudge records the successful call as a marker under `CHECKLIST_STATE_DIR`
(the module's state directory). While it exists the cadence stays
`unattended-checklist`, and `wrap-check` without `--unattended` fails for that
session. A passing `wrap-check --unattended` removes it.

**Unattended runs.** Nobody is present to answer. A step, binding, or profile
step that would need the user's answer (an approval, a choice, a confirmation)
reports `pending` with the answer it needs, writes nothing, and the checklist
continues with the next step. Its items stay carried in the handoff as open
loops. Step 5 still runs and the wrap check still applies.

## The five-step spine

### 1. Reconcile the record — slot `record`

Run the bound command with current session items and matching carried items.
The profile defines the record's stores and policy. If unbound, the step does
not fail: keep its `carries` items as open loops for the final handoff. When
bound, note the UTC time the record write finishes; the wrap check needs it.

A bound record also composes the **session summary** after its ticket step,
once the session's closes are final: milestone and epic outcomes read live from
GitHub first, then each project's goal, what shipped, follow-up tickets, and
decisions. It writes each project's part into that project's session-log entry.
Keep the summary file it names, or the reason it gives for not composing one;
the report starts with it.

### 2. Capture knowledge — slot `knowledge`

Run the binding on new knowledge and matching carried knowledge. Keep anything
pending approval or otherwise unsaved as open loops. If unbound, carry it.

### 3. Fold in lessons — slot `lessons`

Run the bound lesson-routing command on current lessons and carried lesson text.
The default command is `/harness-improve`; a user profile can replace it.
Retain lessons whose routing is pending or failed.

### 4. Persist — slot `persist`

Run the bound command after steps 1–3 and their additions so it includes their
edits. If unbound, retain unsaved items for the handoff. Ticket-state decisions
and merge authorization belong to profiles and their bindings.

### 5. Write and validate the handoff — fixed

After every profile addition, write the interactive session's handoff using the
same checkpoint module as `resume-checkpoint`. Its slash skill is user-only;
call its script directly: `<checkpoint.sh>` is `scripts/checkpoint.sh` in that
skill's real directory (`../resume-checkpoint/scripts/checkpoint.sh` from this
one). Use the actual current session identity in either tool.

```bash
bash <checkpoint.sh> <<'JSON'
<current handoff JSON with remaining carried items and current open loops>
JSON
```

The handoff points to the record and holds what the record does not yet hold.
Preserve unconsumed carried items and their producing identities. Include the
session's uncommitted files through the checkpoint/ledger contract. Never turn
a successful binding invocation into evidence that every input was saved.
Each session item no binding acknowledged goes in `session_items` without
`recorded: true`, so checkpoint carries it as an open loop.

Then run the wrap check with the same profile arguments:

```bash
python3 <checklist-module> wrap-check --user-profile <user-profile> \
  --repository-profile <repository-profile> --written-handoff <handoff-file> \
  --record-at <record-time>
```

`<handoff-file>` is the path checkpoint printed. Checkpoint writes even if
validation fails; the wrap check then fails with the validation errors. When
`record` is bound, `--record-at` is the time of its latest write, or of a later
passing freshness check by the binding;
the wrap check fails when that time is older than the session's last recorded
change (an edited file, published artifact, or owned commit) made before the
latest `plan` start marker. Changes after the marker are the checklist's own
writes, such as persist's commits and lessons edits, and do not count. With no
marker in the ledger, every recorded change counts. When `record` is
unbound, omit `--record-at`; no record check runs. On a non-zero exit, report
each `wrap failed:` reason and refuse to report checklist completion.

Then a profile's `own-worktree` step may remove the session's own linked
worktree; removed earlier, the handoff records no repository and resume cannot
find it. It runs only after the wrap check passes, and only when the worktree is
clean, its branch is merged into the pushed trunk, and a plain
`git worktree remove` succeeds. Otherwise keep it.

## Carried-item contract

At each slot, consume the typed open loops listed in that step's `carried_items`
in the plan **as well as** the current conversation. Each entry has an `index`
into the original resumed handoff and its complete `item`. Pass that item intact,
including its producing session identity and time, a decision's author and source,
lesson text, and artifact location/disposition. Also pass the resumed handoff's
session summaries to a record binding; earlier history belongs to its producer.

Collect explicit saved-item acknowledgements from each binding as JSON:
`{"record": [0, 2], "knowledge": [3]}`. These are indexes into the original
input, not into a shrinking list. Do not acknowledge pending, failed, unsupported,
or merely presented items. If a binding lacks provenance support, report it and
retain the affected loops. Untyped legacy loops remain continuation context.

Use the same profile arguments to settle the input:

```bash
python3 <checklist-module> settle --user-profile <user-profile> \
  --repository-profile <repository-profile> --handoff <resumed-handoff> \
  --receipts <saved-item-receipts.json>
```

The returned JSON removes only acknowledged loops of types the binding consumes.
It preserves every other field and never rewrites the source handoff. Merge its
remaining loops with this session's unsaved items, then checkpoint the result in
step 5 under the current session. Unbound slots carry their items without failing.

## Report

Start the report with the session summary from step 1, unedited, in Claude
Code and Codex alike. When none was composed, start with the one line saying
why. Then name the user and repository profiles resolved. Report each planned step in its
execution order, its bound command if any, and its result: run, carried (an
unbound slot), pending, or failed. Check the results with the same profile
arguments before reporting completion:

```bash
python3 <checklist-module> report --user-profile <user-profile> \
  --repository-profile <repository-profile> --results <results.json> \
  --summary <summary-file> [--unattended]
```

`--summary` is the file the record binding wrote. When it could not compose
one, pass `--no-summary <reason>` with its reason instead. When its output has
no session summary at all (a binding that predates session summaries), pass
`--no-summary "<command> composed no session summary"`; the wrap continues. A bound record with
neither fails the check, so the summary is never silently left out. With record
unbound, omit both: nothing composes a summary, and the output says so. The
output's first field is the summary.

`<results.json>` is an array, in execution order, of
`{"id": <step>, "status": <result>, "writes": [<paths or locations>], "reason": <text>}`.
A pending step needs a `reason` naming the answer it waits for and an empty
`writes`. Every planned step reports, so a pending or failed step never ends the
run early; unattended, the handoff step must have run. On a non-zero exit, fix
the run or report each `report invalid:` reason. Include what was saved, what remains carried, and the final
handoff location, validation result, the wrap check's result, and the own-worktree result:
removed, or kept with the failed check. Profile steps appear at their declared anchors; after
the handoff write, only `own-worktree` and reporting the result follow.
