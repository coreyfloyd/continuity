# continuity

A coding session that ends, compacts, or runs out of context can lose its work:
what changed, where the decisions went, and what still needs attention.
continuity keeps the **record** primary and writes a thin **handoff** pointing
back to it. **Checkpoint** writes that handoff while work is still in progress;
**wrap** finishes the session through a checklist; **pickup** helps the next
session find its way back, in Claude Code or Codex.

![Checkpoint or wrap writes a handoff across the session boundary; pickup in either tool follows it back to the record.](docs/images/session-lifecycle.svg)

The record is your durable home for tickets, project status, documents, and
their history. The package does not provide that home. A handoff carries pointers,
open loops, and the session's uncommitted files; it does not replace the record.
Both tools use the same engines and shared storage. With nothing configured,
handoffs live in `~/.local/state/continuity/handoffs/`, outside either tool's
own data area, and survive adapter removal.

## Skills

Both installers register **seven skills**. Claude Code uses plugin-namespaced
commands; in Codex, invoke the skill by its name, for example `Use checkpoint`.

| Skill name in Codex | Claude Code command | Role |
|---|---|---|
| `checkpoint` | `/continuity:checkpoint` | Write and lint the current interactive session's handoff without finishing its work. |
| `pickup` | `/continuity:pickup` | List handoffs after a cold start, select one, and check it against the record. |
| `checklist` | `/continuity:checklist` | Wrap the session through the spine and profile steps, ending with a validated handoff. |
| `handoff-prompt` | `/continuity:handoff-prompt` | Produce a portable prompt for deliberately transferring a slice of work to another session. |
| `wip` | `/continuity:wip` | Report current work, blockers, and pending approvals from verified state. |
| `improve-harness` | `/continuity:improve-harness` | Route grounded lessons to their owning rules, skills, agent definitions, or memory. |
| `slim-harness` | `/continuity:slim-harness` | Review one instruction source with the user, resolve conflicts, and leave source edits uncommitted. |

`handoff-prompt` produces a prompt to copy; it does not write the stored handoff.
`wip` reports within-session state; `pickup` handles a cold start.

## How the skills fit together

### Context is getting tight

You are midway through a change when the prompt hook reports that context usage
has reached 50%. Reconcile durable progress into your record, then invoke
`checkpoint`. It writes the next action and every unrecorded session item as an
open loop, with uncommitted files drawn from the ledger. It does not commit,
merge, or push. If the handoff fails lint, checkpoint still writes it and reports
the failure. After compaction the start hook restores the handoff as context;
you check its pointers against the record before continuing.

### Finish a task with a profile bound

A test run passes and the tool hook reminds you to wrap. Invoke `checklist`.
It reads your user profile and the additive profiles for repositories you touched,
then runs the five-step **spine** in order:

1. Reconcile the record (`record`).
2. Capture knowledge (`knowledge`).
3. Fold in lessons (`lessons`).
4. Persist (`persist`).
5. Write and validate the handoff (`handoff`).

Suppose your user profile binds `record` to the command that updates your tickets
and documents. That command consumes its acknowledged progress and decisions;
anything it has not acknowledged remains an open loop. Profiles bind the first
four steps, called **slots**, and can add steps without removing the spine.
Lessons defaults to `improve-harness`; unbound slots carry their items rather
than failing. The checklist engine composes and checks the plan; the skill runs
it. Wrap fails if the handoff is invalid or a bound record is stale. A profile's
`own-worktree` cleanup can run only after the handoff check succeeds.

### Pick up in the other tool or worktree

You checkpoint in Claude Code, then start Codex in the same directory and invoke
`pickup`. It reads the same handoff directory. Candidates for this subject come
first, followed by **siblings**: handoffs from other worktrees of the same
repository. Each group is newest first. Choose one and read the record it points
to before acting; starting a session does not authorize its old work queue.
If this directory and its siblings have no candidate, pickup falls back to
local session transcripts from either tool. A handoff is keyed by subject and
session together, so separate sessions do not overwrite each other's files.

### Route a lesson, then slim its source

During a wrap you identify a rule that caused a concrete mistake.
`improve-harness` finds its narrowest owning source and proposes the grounded
change. Its separate size pass flags an instruction file over the running
tool's limit. You choose that file for `slim-harness`, which reads the source
and neighboring instructions, asks about conflicts and produced surfaces,
then applies the agreed edits. It reports numbered changes for rejection and
leaves them uncommitted. It does not automatically slim every flagged file;
unattended lessons remain proposals or carried open loops.

## Hooks

The plugin registers **6 continuity hooks**. These act on interactive sessions;
headless sessions and subagents do not receive the continuity prompts.

| Adapter | When it fires | What you see |
|---|---|---|
| `scripts/post-compact-restore.sh` | Session start, including after compaction | At startup, a count of repository handoffs; after compaction, this session's handoff and a reminder to revalidate it. |
| `scripts/pre-compact.sh` | Before compaction | A reminder to reconcile the record and checkpoint continuation; the raw hook input is backed up. |
| `scripts/context-guard.sh` | Prompt submission | A context warning at 50% or above when usable usage data is available, with instructions to reconcile the record and checkpoint. |
| `hooks/handoff-ledger.sh` | After a file edit or shell tool call | Usually nothing: the ledger tracks touched files and session-owned commits for checkpoint and the stop check. Shell-driven edits need explicit ledger registration. |
| `hooks/handoff-staleness-guard.sh` | Session stop | If no handoff exists, a skeleton is written without blocking. An existing handoff made stale by this session's own work prompts an update and blocks that stop. |
| `hooks/checklist-nudge.sh` | After a shell tool call signaling commit, push, or passing tests | A once-per-session checklist reminder when that wraps a unit of work; configured unattended work uses checkpoint until the whole slate finishes. |

Codex normalizes its hook payloads before calling the same engines and registers
an additional actor guard before shell calls. The guard refuses checklist and
checkpoint writers from headless sessions and subagents. The Claude Code plugin
registers the six continuity hooks without that additional `PreToolUse` guard.
Advisory hook errors exit zero; a valid stop decision can still request a block.
The hooks do not automatically run the five-step wrap.

## Example requests

These natural-language requests work in both tools:

```text
Use checkpoint to write a handoff for this session now.
Use checklist to wrap this task with my profiles.
Use pickup to list what I can resume here, including sibling worktrees.
Use wip to report current work, blockers, and pending approvals.
Use handoff-prompt to prepare a prompt for another session to review this change.
Use improve-harness to route the lesson from the mistake we just corrected.
Use slim-harness to review this instruction file with me.
```

## Architecture

[`docs/architecture/runtime.html`](docs/architecture/runtime.html) is the runtime
architecture map: the two tools, hook adapters, skills, handoff and checklist
engines, profiles, and shared storage. Its boxes link to source files and lines
at a pinned commit. [`runtime.architecture.json`](docs/architecture/runtime.architecture.json)
is its source. Agents and maintainers should read the relevant map before
structural work, confirm it against the code, and regenerate it with the
[archify skill](https://github.com/tt-a1i/archify) after a change moves a box or
an edge. The map leaves out installer trust internals, transcript parsing,
optional session cycling, and the user's record implementation.

For a guided explanation, open the standalone
[presentation](docs/presentation.html), with keyboard and on-screen controls.
The [glossary](docs/glossary.md) defines the vocabulary; [INTERFACE.md](INTERFACE.md)
is the contract for adapter and profile authors.

## Installation and configuration

See [INSTALLATION.md](INSTALLATION.md) for prerequisites, checkout and signed
release routes for both tools, shared configuration and profiles, verification,
uninstall, and test commands.

## License

Apache-2.0. See [LICENSE](LICENSE).
