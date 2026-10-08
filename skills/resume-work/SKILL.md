---
name: resume-work
description: "Resume work after a cold start: default mode lists durable handoff candidates with transcript fallback; --machine reads GitHub claims. Use for 'where was I?' or 'pick up where I left off'; not within-session status."
disable-model-invocation: true
---

# Resume Work

Answers *"where was I?"* from a cold start — no surviving context, no memory of what the last session held. It is handoff-first, with transcript fallback and the existing machine lens.

`<resume-work.sh>` is this package's `scripts/resume-work.sh` (resolve the path from this skill's real directory: `../../scripts/resume-work.sh`).

## Handoff candidates (default)

*"What could I resume on this machine?"*

With no arguments, it reads schema-v2 handoff files from the configured handoff directory (`handoff_dir` in `~/.config/continuity/config.json`; default `~/.local/state/continuity/handoffs/`; `HANDOFF_STATE_DIR` overrides both) and returns candidates in this order: this directory, sibling worktrees whose handoff records the same repository root, then elsewhere on this machine. There is one file per session, named with a readable cwd, cwd hash, and full session id; multiple sessions in one directory are returned individually, newest first. Each candidate carries `session_id`, `headline`, `next_action`, `branch`, and `age`; discovery is file-based, with no index or cross-host sync. A v2 writer records `git.repository_root` as the parent of `git rev-parse --git-common-dir` (the main checkout), never `git rev-parse --show-toplevel`, which is a linked worktree directory.

### Dead-session pruning

The default JSON report includes `prune_candidates` but never changes files. A handoff is a prune candidate when the matching transcript is missing, or when that transcript's mtime is at least 7 days old. Override the threshold with the positive integer `RESUME_WORK_PRUNE_DAYS`. Transcript identity is derived mechanically from the stored handoff subject using Claude Code's cwd slug (`/` and `.` replaced by `-`): `~/.claude/projects/<cwd-slug>/<session_id>.jsonl`.

Each candidate carries `path`, `session_id`, `reason` (`missing_transcript` or `stale_transcript`), and `age`. To act on the suggestion:

```bash
bash <resume-work.sh> --prune
```

`--prune` moves candidates into the handoff directory's `archive/` and reports every move in JSON. It never deletes a handoff, and a same-named archive file is preserved by choosing a numbered destination.

Render these groups for the user and let them choose; do not make another candidate the current task. To select a returned candidate, use its `path`:

```bash
bash <resume-work.sh> --handoff <path>
```

The selected handoff produces a version-2 briefing: handoff text is inferred, while git and best-effort ticket cross-checks are tagged validated or inferred. Ticket lookup degrades cleanly when `gh` is unavailable.

## Transcript fallback

When no handoff candidate is available, the default path reads the on-disk transcript of the most recent OTHER session for this directory, from either provider: Claude transcripts in `~/.claude/projects/<cwd-slug>/`, and root Codex threads whose `session_meta` records this directory under `$CODEX_HOME/sessions/` or `$CODEX_HOME/archived_sessions/` (`CODEX_HOME` defaults to `~/.codex`). The invoking Claude session and the invoking Codex thread (`CODEX_THREAD_ID`) are excluded. Codex subagent threads are not listed. Its normal report carries `source: "transcript_fallback"` and a notice; the existing no-transcript-directory and no-other-session error shapes remain unchanged. An absent store (a provider never used on this machine) is skipped. A Codex `sessions`, dated, or `archived_sessions` directory, or this directory's Claude project directory, that exists but cannot be listed returns `error: "session_store_unavailable"` naming that directory, with a non-zero exit and no session chosen: a session there may be newer. Other Claude project directories are not read by this path.

**Symbolic links.** A configured store root may resolve through a link: `HOME`, `~/.claude`, `~/.claude/projects`, and `CODEX_HOME` are used as given, so a relocated root works. Below that root, a link is rejected wherever the lookup would descend into it or read it as a transcript: Codex `sessions`, `archived_sessions`, any directory in the `sessions` tree, and any rollout file; a Claude project directory the lookup reads (this directory's project on the automatic path, every project under `~/.claude/projects` for `--session`), and any `<id>.jsonl` it reads. That returns `session_store_unavailable` naming the link, with a non-zero exit and no session chosen, on both the automatic path and `--session`. Following the link could read a session outside the store, and skipping it could drop a window of a thread or a second owner of an id. An entry the lookup neither descends into nor reads is not examined, so a linked `memory/` inside a Claude project, a link to a plain file directly under `~/.claude/projects`, or a link to a non-rollout file in the Codex tree, is ignored.

**It always prints a single JSON object — there is no human-rendered mode** (the script emits JSON, the agent renders the briefing, the user never sees raw output). `--json` is accepted for backward compatibility but has no effect on this lens — it's already the only output.

```bash
bash <resume-work.sh> --session <id>
```

| Flag | Effect |
|---|---|
| *(none)* | Handoff candidates; transcript miner only when none are available |
| `--handoff <path>` | Selected handoff briefing with live git and ticket cross-checks |
| `--session <id>` | A specific session from either provider, found wherever it ran (no skip) — see [Cross-tool reconstruction](#cross-tool-reconstruction) |
| `--no-commitments` | `commitments` is `null` instead of collected — skips the ~25 gh round trips it costs |

When neither provider has a session recorded for this cwd, or the only session here is the current one, the output is still a JSON object — `{"version": 2, "error": "no_transcript_directory" | "no_other_session", "message": "..."}` — never a silent fallback to the machine lens's differently-shaped array. Pass `--machine` explicitly for that lens.

### How it picks the session

Newest first across both providers (by file mtime; equal mtimes break by lowest session id, then provider name, never by file listing order), excluding the current session, **skipping any transcript that is malformed or has no substantive assistant turn** — a lone candidate included. A transcript is malformed when any of its files cannot be read, or any non-empty record in any of its files does not decode as a JSON object, the final record included; it is skipped whole, never reconstructed with that record missing. A Codex thread's files are every rollout whose filename carries its id, so a paginated window file that is unreadable, undecodable, or names another thread in its `session_meta` fails the whole thread rather than being left out. When every candidate is skipped, the result is `error: "no_substantive_transcript"` with a non-zero exit and a message naming each skipped file and why. A session that opened and never ran leaves an empty transcript and is not a resume target. If the default lands on the wrong session, pass `--session <id>`.

**A bare `/resume-work <arg>` argument is a session id, not a flag.** Map it to `--session <arg>` before running the script — don't fall through to the no-flag default. On 2026-08-16 `/resume-work 9d6ae6fd` was run against a pre-update copy of this skill that had no directory lens at all; the argument was silently dropped and the machine lens returned an unrelated ticket. The user had to ask why the session id they gave was ignored.

Automatic scoping is by directory, so it naturally separates worktrees — each has its own session set. A worktree with no prior session of its own returns the `no_transcript_directory` error above; that is expected, not a bug. If a store directory this path reads exists but cannot be listed, or is a rejected link (see Symbolic links above), it returns `session_store_unavailable` instead, because an absent session cannot be proven.

### Cross-tool reconstruction

`--session <id>` reconstructs context from a session recorded by **either** Claude Code or Codex, whichever tool runs `/resume-work`, and from any directory. The engine searches both stores for the id and decides the owner from what it finds there — never from the id's UUID version or from `CLAUDE_*`/`CODEX_*` variables:

- **Claude:** `~/.claude/projects/*/<id>.jsonl`, in any project directory.
- **Codex:** rollout files named `rollout-<timestamp>-<id>[_<window>].jsonl` under `$CODEX_HOME/sessions/YYYY/MM/DD/` and the flat `$CODEX_HOME/archived_sessions/`. The id is the thread id (`session_meta.payload.id`), never `payload.session_id`, which names the parent thread for a subagent. A thread split across several files (a paginated window) is read as one source, oldest file first.

The report is the same version-2 object for both providers, plus `provider` (`"claude"` or `"codex"`) and `transcript_files` (the file or files read, in order). Codex records are mapped to the same fields: user turns from `UserMessage` items, the `final_answer` message as the sign-off, each command as a `Bash` call with its output, and each file change as an edited path. The branch comes from the rollout's `session_meta.git`, since Codex records no per-turn branch.

An explicit id never falls back to a guess. Each failure is a JSON error object with a non-zero exit:

| `error` | Meaning |
|---|---|
| `no_such_session` | Both stores exist, every directory in them could be searched without meeting a rejected link, and neither holds the id. The message lists every root searched. |
| `session_store_unavailable` | Either (a) the id was not found and a provider's store is absent (no `~/.claude/projects`, or no `sessions`/`archived_sessions` under `CODEX_HOME`): point `HOME`/`CODEX_HOME` at the store that holds it. Or (b) a store directory exists but cannot be searched: `~/.claude/projects`, any project directory under it, or any Codex `sessions`, dated, or `archived_sessions` directory. Or (c) the lookup met a rejected link (see [Symbolic links](#transcript-fallback)): a linked project directory or `<id>.jsonl`, or a linked Codex store directory or rollout file. Cases (b) and (c) fail even when a valid match was found elsewhere, since the unsearchable directory or the link may hold another member of the thread or a second owner of the id. The message names the directory or link and never carries transcript content. One unsearchable or linked Claude project directory blocks every explicit lookup on the machine until its permissions are fixed or the link is replaced by a real directory. A project directory counts as searchable when the exact `<id>.jsonl` name can be checked in it. |
| `ambiguous_session` | The id is a valid transcript in more than one place (both providers, or two Claude project directories). The message names each file. |
| `invalid_session_transcript` | A file matched the id but cannot be read as a transcript of its provider, including when it cannot be read or any non-empty record in it, the final one included, does not decode as a JSON object. This fails even when another match is valid. |
| `invalid_session_id` | The id contains path or glob syntax. |

**This is reconstruction, not native continuation.** `/resume-work` rebuilds what a dead session did so the current tool can carry on; it works across providers. Continuing the provider's own conversation thread — `claude --resume <id>` or `codex resume <id>` — reads only that provider's store and remains a same-provider capability.

### Rendering the morning briefing (`version: 2`)

The JSON is a deterministic worklog that YOU, the agent, turn into the user's actual briefing. **Rendering it is your job, not the script's.** **Every open ask the dead session left is re-rendered in full** — context, options with tradeoffs, recommendation — never as the prior session's `R3`/`O2` codes: the user has no memory of those labels, and a briefing that says "R6: release, manual, or arm" cannot be answered (2026-09-05).

Top-level fields: `session`, `provider`, `transcript_files`, `messages`, `first_ts`, `last_ts`, `first_user` (uncapped), `last_user` (uncapped), `last_assistant` (uncapped, selected by `stop_reason == "end_turn"` — the sign-off, not just the last row with any text), `ticket_refs`, `git` (session-branch cross-check: `branch`, `head`, `exists`, `on_main`, `unmerged`, `ahead_behind`), `branches`, `tickets`, `files_by_repo`, `staleness`, `skipped_candidates`, `session_repo_cwd`, `session_repo_readable`, `commitments` and `commitments_reason` (see [COMMITMENTS](#commitments) below).

**Exit code and error shape.** The directory lens is one Python process (`skills/resume-work/scripts/resume_work.py`); `scripts/resume-work.sh` parses arguments, finds the engine, and runs it. Every failure path puts `{"version": 2, "error": ..., "message": ..., "commitments": null, "commitments_reason": "lens_error"}` on stdout — the shape never varies.

**Branch on the `error` key, not on the exit code**, because the two do not line up and are not meant to:

| Situation | `error` | Exit |
|---|---|---|
| Normal report | absent | 0 |
| `no_transcript_directory`, `no_other_session` (automatic path) | present | **0** |
| An explicit `--session` that did not resolve (see [Cross-tool reconstruction](#cross-tool-reconstruction)) | present | non-zero |
| `session_store_unavailable` on the automatic path (a store directory exists but cannot be listed, or a rejected link) | present | non-zero |
| Engine crashed, printed non-JSON, or exited non-zero | present | non-zero |

"Nothing here to resume" is an answer, not a failure — a worktree with no prior session of its own returns `no_transcript_directory` and exits 0 by design. A non-zero exit means the lens could not produce a trustworthy answer: it broke, a selected transcript was malformed, or a store directory could not be listed or was a rejected link.

**`session_repo_readable: false`** means the session's last recorded cwd no longer exists on disk — every branch in `branches` will read `deleted_no_merge` (the highest alarm) and `files_by_repo` will be empty, not because that is what happened, but because git itself could not be read from there. Check this field before treating an all-red report as a resolved finding.

| Key | What it is |
|---|---|
| `branches[]` | Every branch/worktree the session touched. Each entry: `branch`, `worktree_path`, `worktree_exists`, `dirty`, `ref` (`local`/`remote`/`none`), `on_main`, `merge_evidence` (the `%h %s` line of the merge commit, when one was found by name), `state`, `alarm_rank`, `commits`, `commits_source`. `state` is one of `dirty`, `clean_unmerged`, `gone_unmerged`, `merged`, `landed`, `deleted_no_merge`, or **`unknown`** — `unknown` means git itself could not be read for that worktree (a real error, not "nothing to report"). `alarm_rank` is the machine-readable severity order (**lower = more urgent**: `unknown`/`dirty` = 0, `deleted_no_merge` = 1, `clean_unmerged`/`gone_unmerged` = 2, `merged`/`landed` = 3) — sort `branches[]` by it rather than re-deriving an ordering from this prose. |
| `branches[].commits_source` | Why `commits` is what it is: `"direct"` (the `main..branch` range, authoritative — an empty list here for an unmerged branch is a TRUE empty), `"merge_evidence"` (the branch is merged/gone and its own merge commit was found by name), `"not_applicable"` (ref is gone with no merge found — this already IS the `deleted_no_merge` alarm), or **`"unresolved"`** (the branch is merged, its ref still exists, but no merge commit could be found by name — its `state` is correctly `"merged"`, but what it CONTAINED is genuinely unknown, not confirmed empty). Never render an `"unresolved"` branch's empty `commits` as "no changes." |
| `tickets` | Every ticket this session touched, ranked by **write ops** (comment/edit/close/reopen/create) — not view/mention count. Each entry: `repo`, `number`, `ops` (per-verb counts), `write_ops`, `mentions`, `created`. A `gh issue create` never prints its number in the command itself, only in the tool result on success — `created: true` means that recovery succeeded. |
| `files_by_repo` | Per repo (keyed by repo root, not per-worktree path): `committed`, `uncommitted`, `transcript_scraped` (three separate lists — a path can appear in more than one), and `unresolved_branches` (branch names in this repo whose `commits_source` is `"unresolved"` — their real files are absent from `committed` above, not confirmed empty; cross-reference before treating the file list as complete). Uncommitted alone misses a merged-but-still-open branch's contribution; committed alone misses live uncommitted edits (a real session had 5 uncommitted changes in a notes repository that `git log` reported as zero); transcript-scraped catches bypass-mode edits routed through `Bash` (heredocs, `sed`, `python3 -c`) that never went through `Write`/`Edit` at all. |
| `staleness` | `last_end_turn_ts` (the last message actually sent to the user) plus `tool_calls_after` — nonzero means the session kept working (or crashed mid-tool-call) past its last sign-off, and `last_assistant` above is stale by exactly that gap. |

**Render the user's briefing from this JSON, not from your own recollection of the session** (subagent tool calls are invisible to the transcript — see Known limit below — so re-derive everything from what the JSON actually contains):

> **Last night** — what it set out to do (`first_user`), what it did (`last_assistant`, `tickets`, `branches[].commits`)
> **Landed** — merged/closed tickets and branches
> **Outstanding** — unmerged branches, uncommitted files (`files_by_repo`), open claimed tickets *(the section that matters most)*
> **Start here** — one ticket, one command: the top of `tickets` (already ranked by write-ops) joined with any row in `commitments`; a branch still `dirty`/`clean_unmerged`/`gone_unmerged`/`unknown` in `branches` is a second line, not the headline.

**Say why the top ticket existed.** Read its body for its origin ("found while gating X", a `blocked_by`, a parent issue) and state in the briefing whether that parent is still open. A session that worked a blocker ticket otherwise reads as the main task (2026-09-27: a ticket filed while gating another, already closed; the user asked twice what the session's real scope was). **Read the parent's body and its open acceptance criteria before proposing any transition on it.** A parent that still has unbuilt criteria is not "done once the blocker lands"; offering to close it from its title and labels is a state claim made without the read (2026-09-28: a resume briefing offered "close it as done" for a parent with three unbuilt criteria; the user asked why).

**Tag every line** `(validated: <method>)` or `(inferred: <basis>)` — not just the uncertain ones. `(validated: git log)` for a landed branch, `(validated: gh issue view)` if you re-checked ticket state live, `(inferred: last assistant message, unverified since)` for anything you are relaying from `last_assistant`/`tickets` without re-confirming it. The JSON's own claims are exactly as stale as the transcript that produced them — a resumed session's "test X fails", "Y is missing", "Z is blocked" may have been fixed by someone else in the interval since the transcript was written. Run the test, grep for the file, read the ticket live before treating any of it as current. (2026-08-30: a resumed session reported one test failing on main; it had been fixed two days earlier by `d139e84d`. Acting on it directly would have meant debugging a passing test.)

**A staged AFK-dispatch brief is as stale as the findings — re-verify its premises before dispatching.** When the resumed session left a pre-written dispatch staged (worktrees created, a brief on disk, scope registered), the brief is not a green light: re-verify its premises against current main first — every target file/line it names still exists, and every "assume X does not exist / use the old path" claim still holds. A brief can contradict code that landed after it was written, **even code that existed at its own staging base**. (2026-09-21: a day-old brief said "assume no replace endpoint, use add-then-remove" while the `consolidate` endpoint was already merged and wired at the brief's own staging base; two of the ticket's three ACs had shipped under sibling tickets. Dispatching it would have wasted an AFK launch or rebuilt done work — live verification re-scoped it to the one AC actually left.)

**The branch cross-check keys on the resumed session's own branch** — its last recorded `gitBranch` — not on the working tree's current branch. In a shared checkout a concurrent session swaps the checked-out branch, so `$PWD` HEAD is unrelated to the session being resumed; reading it reported unmerged work as "already on main (merged)". Fixed 2026-08-16.

### Known limit: subagent work is invisible here

Zero `isSidechain` rows were found across 15 sampled sessions, in a session that made a dozen `Agent` tool calls. Everything a dispatched subagent did — commits it made, tickets it touched, files it edited — is absent from `tickets`, `branches[].commits`, and `files_by_repo` unless it was already **committed** by the time this runs (`commits_source: "merge_evidence"` and A6's `git log`/`git status` pick up committed work regardless of who wrote it). The transcript itself will never carry it — don't imply completeness in the rendered briefing when the session's own text mentions subagents it dispatched.

### Trunk branch

Every `main`-relative comparison resolves the trunk branch in this order: the `RESUME_WORK_TRUNK` env var, if set; otherwise `git symbolic-ref refs/remotes/origin/HEAD` (deterministic — the one branch the remote itself designates as default, empty when unset, never a wrong guess); otherwise `main`. A repo whose trunk is `master`/`trunk`/`develop` needs neither set as long as its `origin/HEAD` is configured (true for most clones); `RESUME_WORK_TRUNK` remains the override for a repo with no remote HEAD at all, or to force a value.

## COMMITMENTS

A field in the same JSON object, `commitments` — not a separate printed section (fixed in #910: it used to be prose appended after the JSON, which made the default invocation unparseable — `json.load` failed with "Extra data" past the report's closing brace). It answers a different question from everything else in the object: not *what did the last session say*, but **what did it promise**. The scan runs inside `resume_work.py` alongside everything else in this lens; it was a bash function whose output was merged into the report through an environment variable until #910 round 4, and every open fail-mode defect sat on that seam.

The transcript recovers narrative. Commitments — "verify the 21:30 nightly", "watch #820 for 7 days", "delete the preview page once the nightly lands" — are not narrative and are usually not in the tail of the transcript at all. `/record-update`'s **commitments sweep** stamps them onto tickets before the session dies; this field is the read side.

**Three distinct values, not two** — collecting nothing and not attempting the collection are different statements, and a silently absent field would read exactly like "you promised nothing" when it might mean "never checked":

| Value | Meaning |
|---|---|
| `null` | Not collected. `commitments_reason` says why. |
| `[]` | Collected, and nothing matched. |
| `[{...}, ...]` | Collected; one object per ticket carrying this session's claim. |

**`commitments_reason` is the companion field, and `null` on it means the array is complete.** Read it before rendering the Outstanding section — an empty array with a reason is not the same claim as an empty array without one.

| `commitments_reason` | Meaning |
|---|---|
| `null` | Collected, complete. The only value that lets you say "nothing outstanding." |
| `"skipped"` | `--no-commitments` was passed. Nothing was checked. |
| `"no_session_id"` | The resumed session's id could not be determined, so the join had no key. |
| `"no_owner"` | No GitHub owner is configured (`RESUME_WORK_COMMITMENT_OWNER`, else `commitment_owner` in the continuity configuration), so there is no owner to scope the search. Nothing was checked. This is the default. |
| `"gh_not_found"` | `gh` is not on `PATH`. |
| `"gh_search_failed"` | `gh` ran and failed — stale auth, offline, rate limit. **This is the cold-start case**, and until #910 round 4 it reported `[]`, a false "you promised nothing". |
| `"partial_claim_fetch"` | The array is real but INCOMPLETE: at least one ticket's claim comment could not be read, so its inheritance is unknown rather than absent. |
| `"internal_error"` | An unexpected failure inside the commitments path. The rest of the report is still valid. |
| `"lens_error"` | The lens never got far enough to scan (no transcript directory, engine failure). Accompanied by a top-level `error`. |
| `"handoff_not_collected"` | A selected handoff has no transcript claim scan; read its live ticket cross-check instead. |

Each object: `repo`, `number`, `title`, `status_labels` (the ticket's `status/*` labels, e.g. `["status/deployed"]`), `handoff_pending` (`true` when the commitments sweep stamped `state: handoff-pending-successor`; `false` means this session simply held the ticket), `branch` (or `null`), `url`.

**Scoped by claim, not by status label.** `status/next` is a repo-wide queue — with several sessions in one repo a successor cannot tell its inheritance from the general pool. The claim comment already binds host · session · branch, so the query is: every **open** issue labeled for this host whose claim comment names the **resumed session's id**. Any status, so a `status/next` or `status/deployed` commitment shows up where the machine lens (`status/active` only) would never see it.

### The successor re-claim rule

Re-claim each row under **your** session id with the claim tool your setup uses; the package ships none. Succession is the one legal claim-over-claim takeover: the re-claim is what *retires* the handoff marker, since a claim by a different session clears `state:` while a heartbeat deliberately preserves it, so the marker survives until someone actually inherits it. Without a claim tool, state in the briefing which rows you are taking over and leave the markers alone.

### Bounds and known gaps

- Scans the **25 most recently updated** open host-labeled issues owned by `RESUME_WORK_COMMITMENT_OWNER` (`RESUME_WORK_COMMITMENT_LIMIT` overrides). Claim bodies are fetched by a thread pool whose default size is that same limit, i.e. one wave (`RESUME_WORK_COMMITMENT_JOBS` overrides, capped at 32 — lower it on a rate-limited box). Measured ~14s uncapped serially, which is what `--no-commitments` exists to skip.
- **The join is a 12-char prefix.** The claim tool is expected to truncate the session id; the transcript id is a full uuid. A session that claimed under `CLAUDE_SESSION_NAME` (a name, not an id prefix) is **not** recoverable by id.
- **A stale row is possible.** `heartbeat` keeps the recorded session id when the host matches, so a ticket stamped by session A and later heartbeated by session B on the same box still reads A. Verify a row before inheriting it — same rule as the transcript findings above.

## Machine lens (`--machine`)

*"What does this HOST hold?"* — an optional lens, available when a machine-lens command is configured (`RESUME_WORK_MACHINE_LENS`, else `machine_lens` in the continuity configuration; without one, these flags report `machine_lens_unavailable`). The command a claims setup supplies reads the **claim comments** its claim tool writes onto tickets (host · session · branch · started · heartbeat) and renders them stalest-first. Use it to spot work stranded on another machine.

```bash
bash <resume-work.sh> --machine
```

| Flag | Effect |
|---|---|
| `--machine` | This machine only — filters on the matching `host/*` label |
| `--all-hosts` | Every host (spot work stranded on a Mini) |
| `--host <name>` | A specific host |

- **Stalest first** — the thing dropped longest ago is the one most likely abandoned rather than paused.
- **`(!)` past 48h** is the claim TTL. Past it the claim is probably stale; a claim sweeper, where one runs, reports these.
- **`never heartbeat`** — claimed but no commit/push/test ever bumped it.

**Caveat — the claim plumbing under-fires.** A session that resumes an already-active ticket never emits a claim, and the heartbeat safety net is unreliable (tracked in the claim/heartbeat determinism bug). So the machine lens can show nothing even when work is in flight. The directory lens does not have this gap — it reads the transcript directly. When the directory lens finds no other transcript, it reports that as a JSON error object (see above) rather than silently falling back to this lens — pass `--machine` yourself to check it.

## Then resume properly

The output tells you *which* ticket and *where* the session stopped. The ticket's progress comments are the durable in-flight record (#386 layer 4):

```bash
gh issue view <n> --repo <repo> --comments
git status && git log --oneline -5
```

Check out the branch named in the row, and confirm its state before assuming the summary is still accurate.

## Distinct from `/wip`

`/wip` is a within-session snapshot seeded by the SessionStart ticket injection and verified against live reality — "what is in flight *for this session*". `resume-work` needs no surviving session: the directory lens reads a dead session's transcript, the machine lens reads GitHub. Run `resume-work` first in a fresh terminal; run `/wip` once you are back inside a session with context.

## Related

- The `record` slot's binding, through the checklist's commitments sweep, is the write side of the COMMITMENTS section.
- `/wip` — the within-session counterpart
