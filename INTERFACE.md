# Interface

What an adapter, a profile author, or another tool may rely on. Anything not listed here is
internal and may change. The vocabulary is defined in `docs/glossary.md`.

Paths below are relative to the repository root. An adapter locates the package by the
directory it installs it into, never by importing it: every entry point is a command.

## 1. Storage

| What | Default | Override |
|---|---|---|
| Handoffs | `~/.local/state/continuity/handoffs/` | `HANDOFF_STATE_DIR`, else `handoff_dir` in the configuration |
| Ledger (one file per session) | the parent of the handoff directory | `HANDOFF_LEDGER_DIR` |
| Pre-compaction backups | `~/.local/state/continuity/compact-state/` | `HANDOFF_COMPACT_STATE_DIR` |
| Checklist state (the unattended-wrap marker) | `~/.local/state/continuity/checklist/` | `CHECKLIST_STATE_DIR`, else `checklist_state_dir` in the configuration |

None of these is inside a tool's own data area (`~/.claude`, `~/.codex`, a plugin's data
directory), so removing an adapter never removes a handoff. Both tools read and write the
same directory, so a handoff written under one is listed by the other.

A handoff is one JSON file, `schema_version` 2, named
`<readable-subject>-<12-hex-subject-hash>-<session-id>.json`. The subject and the session
together identify it, so two sessions on one subject never overwrite each other. The
`subject.working_directory` is a canonical path, and `git.repository_root` is the parent of
`git rev-parse --git-common-dir`, so every worktree of a repository shares one root.

The session id comes from `--session-id`, else `CLAUDE_SESSION_ID`, else
`CLAUDE_CODE_SESSION_ID`, else `CODEX_THREAD_ID`.

## 2. Configuration

One JSON object, read from `$CONTINUITY_CONFIG`, else
`${XDG_CONFIG_HOME:-~/.config}/continuity/config.json`. The same file serves both tools. An
absent file is an empty configuration. A file that is unreadable, not an object, or holds a
wrongly typed value is an error, reported by the command that read it. It is never read as
an empty configuration.

| Key | Type | Meaning |
|---|---|---|
| `handoff_dir` | string | Where handoffs are stored. `~` is expanded. |
| `checklist_state_dir` | string | Where the checklist keeps its marker. |
| `checklist_surfaces` | list of strings | Standing surfaces the surface detector reports in addition to the session's own repository. |
| `checklist_suite_baselines_dir` | string | Directory of per-repository suite baseline files the surface detector reports. |
| `commitment_owner` | string | The GitHub owner whose issues carry claim comments. Without it `resume-work` reports commitments as `no_owner`. |
| `machine_lens` | string | A command `resume-work --machine` runs, given `RESUME_WORK_WANT_HOST` and `RESUME_WORK_AS_JSON` in its environment. Without it the flag reports `machine_lens_unavailable`. |
| `host` | string | The host name the commitments scan filters on, unless `RESUME_WORK_HOST` is set. |
| `cycle_sessions` | object | Maps a session name to a directory under the home directory, for a consumer that cycles sessions (section 8). |
| `unattended` | object | Optional unattended mode, section 6. |

`python3 scripts/lib/handoff.py config-get <key>` prints one value, a list one item per line,
and exits 2 with the reason on stderr when the configuration is unreadable or the value is
mistyped.

## 3. Handoff engine: `scripts/lib/handoff.py`

Standard library only. Every command reads plain arguments, the environment, and stdin, and
writes stdout. Exit status: 0 done, 1 for a negative answer (`repository-root`,
`is-interactive`), 2 for a refused or failed operation. The hook adapters exit 0 whatever their command
returned, so a broken hook never blocks a session.

| Command | Purpose |
|---|---|
| `resolve [cwd] [--subject] [--session-id ID]` | Print the handoff path for a directory and session. |
| `repository-root [cwd]`, `count [cwd]` | The shared repository root, and how many handoffs exist for it. |
| `checkpoint [--cwd D] [--session-id ID] [--trigger T]` | Read continuation JSON on stdin, compose subject and git, lint, write atomically, print the resume paste block. Writes even when the lint fails and reports the failure. `T` is `checkpoint`, `wrap`, or `manual`. |
| `write PATH` | Low-level atomic write. Refuses a payload that fails the lint. |
| `lint [PATH] [--session-id ID]` | Validate a handoff. `scripts/lint-handoff.sh` is a thin adapter over this command. |
| `ledger-file PATH --cwd D --session-id ID` | Record a file a shell-driven edit changed. |
| `ledger-artifact --location L --disposition keep\|remove [--status pending\|recorded\|removed] --session-id ID --cwd D` | Record, link, or retire a published artifact. The one command any publishing tool calls. |
| `is-interactive [JSON]` | Exit 0 for an interactive session, 1 for a headless entry point or a subagent (`agent_id` in the payload). |
| `state-dir`, `config-get KEY` | Where handoffs are stored, and one configured value. |
| `migrate` | Rename handoffs keyed by directory alone to their per-session path. Idempotent. |
| `post-compact`, `pre-compact`, `context-guard`, `ledger-record`, `guard-decide` | Hook decisions, section 5. |
| `cycle-request-*`, `automated-cycle-handoff` | Support for a consumer that cycles sessions, section 8. |

`scripts/lib/handoff.sh` is a sourceable shell library over the same commands:
`handoff_checkpoint`, `resolve_handoff_path`, `handoff_repository_root`, and the rest.

A checkpoint's input is the continuation JSON described in `skills/resume-checkpoint/SKILL.md`:
`resume.headline`, `resume.next_action`, `resume.session_summary`, `resume.open_loops[]`,
`spawned_processes`, and `session_items[]`. Each session item has a `type` (progress,
commitment, decision, knowledge, lesson, artifact, or open_loop), `context` (at most 600
characters), `disposition`, the producing `session_id`, and `produced_at`. A decision also
has `author` and `location`; an artifact has `location` and a `keep` or `remove`
disposition. An item carried in a handoff keeps its producer's identity and date until a
slot binding consumes it.

## 4. Resume: `scripts/resume-work.sh`

Prints one JSON object. With no arguments it lists handoff candidates for the current
directory (siblings marked), falling back to recent sessions from the tool's own history.

| Option | Effect |
|---|---|
| `--handoff PATH` | A briefing for one handoff. |
| `--session ID` | A specific transcript, from either tool. |
| `--prune` | Archive handoffs whose session is gone. Never deletes. |
| `--no-commitments` | Skip the claim scan, which costs one `gh` call per ticket. |
| `--machine`, `--all-hosts`, `--host NAME`, `--json` | The optional machine lens, supplied by configuration. |

Every failure prints the same object shape, `{"version": 2, "error": CODE, "message": TEXT,
...}`, and exits non-zero. Codes include `usage`, `handoff_config_error` (the configuration
is unreadable or mistyped), `machine_lens_unavailable` (no lens configured),
`engine_not_found`, `engine_failed`, and `engine_bad_output`. `RESUME_WORK_ENGINE` names the
engine (`skills/resume-work/scripts/resume_work.py`) when an adapter installs it away from
the scripts.

Other settings: `RESUME_WORK_HOST`, `RESUME_WORK_COMMITMENT_OWNER`,
`RESUME_WORK_COMMITMENT_LIMIT`, `RESUME_WORK_COMMITMENT_JOBS`, `RESUME_WORK_TRUNK`,
`RESUME_WORK_PRUNE_DAYS`, `RESUME_WORK_MACHINE_LENS`. The package ships no claim tool.
Claims, if any, come from the user's own setup.

## 5. Hooks

Six hook moments, each a bash adapter that pipes the hook payload on stdin to an engine
command and exits 0. An adapter registers each in its tool's own format.

| Moment | Adapter | Engine command |
|---|---|---|
| Session start | `scripts/post-compact-restore.sh` | `handoff.py post-compact` |
| Before compaction | `scripts/pre-compact.sh` | `handoff.py pre-compact` |
| Prompt submit | `scripts/context-guard.sh` | `handoff.py context-guard` |
| After a tool call | `hooks/handoff-ledger.sh` | `handoff.py ledger-record` |
| Stop | `hooks/handoff-staleness-guard.sh` | `handoff.py guard-decide` |
| After a tool call (shell) | `hooks/checklist-nudge.sh` | `checklist.py wrap-cadence` |

The payload fields the engine reads: `session_id`, `cwd`, `source` (session start;
`compact` after a compaction), `transcript_path` (prompt submit), `tool_input` and
`tool_response` (after a tool call), `stop_hook_active` (stop), and `agent_id` (present only
for a subagent). Hooks act only for an interactive session: a payload with `agent_id`, or an
entry point in `sdk-cli`, `sdk-py`, `sdk-ts`, `sdk`, `mcp`, or `headless`
(`CLAUDE_CODE_ENTRYPOINT`), gets no output.

`hooks/checklist-actor-guard.sh` is an optional seventh adapter. Registered before a shell
tool call, it denies a subagent or headless session the checklist and the checkpoint
writers, using `checklist.py actor-guard`.

Hook output is JSON in the tool's hook format or plain text context. The stop hook prints a
block decision only when the session has an owned commit since the handoff's baseline, or an
edited file newer than the handoff.

## 6. Checklist engine: `scripts/lib/checklist.py`

Standard library only. The skill (`skills/checklist/SKILL.md`) runs the plan; the engine
composes and checks it. It writes no handoff and runs no skill.

| Operation | Purpose |
|---|---|
| `plan [--user-profile P] [--repository-profile P ...] [--handoff H] [--unattended] [--session-id ID]` | Compose the spine with the profiles and route a resumed handoff's carried items to their slots. Writes a start marker to the session's ledger. |
| `settle ... --handoff H --receipts R` | Remove the loops a binding acknowledged. Never rewrites the source handoff. |
| `wrap-check ... --written-handoff H [--record-at T] [--unattended]` | Fail, with the reason, when the handoff is invalid, or when `record` is bound and its latest write is older than the session's last change. |
| `report ... --results R [--summary F \| --no-summary WHY] [--unattended]` | Check a run's step results; the report starts with the session summary. |
| `wrap-cadence` | Read a hook payload on stdin and print `none`, `checkpoint`, `unattended-checklist`, or `checklist`. |
| `actor-guard`, `is-completion-command` | Hook helpers. The first prints a deny decision; the second exits 0 when the command completes an unattended slate. |

**The spine** has five steps in this order: `record`, `knowledge`, `lessons`, `persist`,
`handoff`. The first four are slots. `lessons` is bound to `/harness-improve`, consuming
`lesson` items. Unbound slots are carried: the items they would consume stay in the handoff
as open loops. `handoff` is not a slot and is always last.

**A profile** is JSON, or Markdown with one fenced block tagged `checklist-profile`:

```json
{
  "name": "example",
  "bindings": {"record": {"command": "/record-step", "consumes": ["progress", "decision"]}},
  "steps": [{"id": "tidy", "after": "persist", "instructions": "..."}]
}
```

A binding names a `command` and the item types it `consumes`. A step has a unique `id`, an
`after` anchor (`before-spine`, `record`, `knowledge`, `lessons`, or `persist`), and
`instructions`, or a `section` naming a level-two heading in the same Markdown file. A user
profile applies to every session; a repository profile only adds steps. A profile that
removes, skips, or replaces a spine step, or adds a step after `handoff`, is rejected with a
message naming the step. User profile: `$CHECKLIST_USER_PROFILE`, else
`${XDG_CONFIG_HOME:-~/.config}/checklist/profile.md`.

**Unattended mode** is optional and absent by default. A user who launches sessions and
leaves them to execute approved work describes that mode in the `unattended` section:

| Key | Meaning |
|---|---|
| `state_module` | A Python file exposing `state_path(session_id)` and `read_state(path, session_id)`. The state is a mapping with `mode` (`"afk"` while the slate is running), `updated_by`, and `updated_at`. |
| `completion_command` | A regular expression matching the command that completes a slate. |
| `completed_by` | The `updated_by` values that command leaves in the state. |

While the state module reports `afk`, `wrap-cadence` answers `checkpoint`. After the
completion command succeeds it answers `unattended-checklist` until a passing
`wrap-check --unattended`. Without the section every interactive session is `checklist`.

## 7. Skills

Six skills, each a directory with a `SKILL.md`. An adapter installs them where its tool
looks for skills. Each resolves its scripts from its own real directory, never from a
tool-specific home directory.

| Skill | Role |
|---|---|
| `resume-checkpoint` | Checkpoint the current session. `scripts/checkpoint.sh` is the entry point the checklist also calls. |
| `resume-work` | Resume after a cold start. Its engine is `scripts/resume_work.py`. |
| `checklist` | The wrap-up spine. |
| `handoff-prompt` | A copy-pasteable prompt that hands a slice of work to another session. |
| `wip` | In-flight work, blockers, and pending approvals for the current session. |
| `harness-improve` | Route a lesson to the user's rules, skills, agent definitions, or memory. |

## 8. For consumers that cycle sessions

Session cycling is not part of the package. A consumer that cycles sessions reaches the
engine through commands and the configuration, never by import: `cycle_sessions` in the
configuration, and `cycle-request-path`, `cycle-request-write`, `cycle-request-validate`,
`cycle-request-dispatch`, and `automated-cycle-handoff` on the handoff engine.

## 9. Checking for personal references

`scripts/lib/reference_check.py --terms FILE PATH...` reports every occurrence of a listed
term, case-insensitively, as `path:line:column: term: text`. Exit status 0 is clean, 1 has
findings, 2 is a usage error. The term list is supplied by the caller and is not part of this
repository. `tests/test_shipped_surface.py` runs the check over the whole tree when
`REFERENCE_CHECK_TERMS` names a list.
