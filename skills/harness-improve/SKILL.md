---
name: harness-improve
description: "Fold what a session taught into rules files, skills, agent definitions, or memory, with one placement per lesson. Use at wrap-up, or when the user says \"from now on\", \"remember this\", \"make this a durable rule\", \"always/never do X\", or asks where a rule belongs. Interactive: proposes, then waits. Unattended: edits nothing."
---

# Harness Improve

The **inward-writing** wrap-up pass. The record holds what this session *did*; this skill folds what the session *taught* back into the instructions that will govern the next one. It is also the place to decide where a single durable rule belongs when the user asks for one mid-session.

Rules, skills and agent definitions, and memory are covered in one pass, because the interesting decision is shared: **where does this lesson belong?** Answering that separately per surface means several scans of the same transcript and constant hand-offs.

## Routing: the user's file, or the default

Look for a **routing file**: `$HARNESS_IMPROVE_ROUTING` when set, otherwise `harness-improve/routing.md` under `$XDG_CONFIG_HOME` (default `~/.config`). When it exists, read the whole file: it **replaces** the default routing table below, and it may also name where the sources live, where unattended proposals are filed, recording conventions, and extra steps for this procedure. Follow it wherever it differs from this skill. When it does not exist, use the default routing; it needs no configuration.

### Default routing

Make the placement call **once per lesson**, then write to whichever surface won. Prefer the narrowest surface that still loads when the lesson is needed: always-loaded files cost every future session context.

| A lesson is… | It goes to |
|---|---|
| A deterministic "always / whenever / before X" the harness can enforce | A **hook** in the tool's settings. Text is a request; a hook fires |
| Something that matters only while running one skill | **That skill** (its steps or gotchas) |
| Behavior of one agent type | **That agent's definition** |
| A constraint of one repository | **That repository's instruction file**: `AGENTS.md` when present (both tools read it), else `CLAUDE.md` |
| Something every session must obey, in any repository | **The user-level rules file** (`~/.claude/CLAUDE.md` for Claude Code, `~/.codex/AGENTS.md` for Codex; both when it binds both tools). Last resort for rules |
| A cross-session fact the agent needs that is not a rule and has no narrower trigger | **Memory** (the tool's memory store). Never for rules |

**Memory is never for rules.** A memory that instructs future behavior ("always X", "before Y do Z") was misfiled; place it up the table and delete the memory.

**Recording form.** Write each rule as the rule plus a one-line why with its date and source, and keep inline whatever mechanism is needed to apply it (the exact command or construct). One home per rule: other sites get a one-line pointer if discovery matters, never a second copy. A pointer to the canonical home beats a restatement.

Fixing an *existing* rule edits it where it already lives; placement applies to new lessons, or to proposing a **move** when something is clearly in the wrong place. Flag moves explicitly; never silently relocate.

**A single rule asked for mid-session** ("from now on…", "remember this") gets the same lookup: place it, state the placement (which surface, which trigger) so the user can redirect, and follow the mode rules below.

## The one rule that makes this safe: every edit must be grounded

A proposed edit is legitimate only if you can point at the **specific moment in this conversation** that motivates it. The failure mode is generating plausible-sounding improvements traceable to nothing — that is how rules files bloat and skills drift from how they actually behave.

Legitimate motivations:

- **A rule or skill led you wrong** — you followed it as written and the user corrected the outcome.
- **An error hit** — a command failed, a path was stale, a step didn't work as written.
- **A missing step** — you had to figure out something the instructions should have told you.
- **A repeated instruction** — the user stated a preference or convention that nothing records.
- **A stale reference** — a named path, flag, command, or label vocabulary no longer exists.
- **A contradiction** — two rules, or a rule and reality, conflict and this session had to resolve it.
- **A trigger miss** — a skill should have fired (or fired wrongly) for what the user asked.
- **A memory that failed verification** — a recalled fact that no longer holds.

If you can't cite the moment, don't propose the edit. "This could be clearer" with no incident behind it is not grounds. A rule the user dictates is its own grounding.

## Mode — decide this first

| Mode | Detection | Behavior |
|---|---|---|
| **HITL** | interactive session the user is present in | Proposal table → **wait** → apply approved edits |
| **AFK** | the user left the session to run unattended, or it is a scheduled or pipeline run | **Edit nothing.** Record proposals durably — see below |

The AFK branch exists because the alternative is worse than doing nothing: an unattended session either stalls forever on an approval that will never come, or self-approves its own edits to the files that govern every future session. Neither is acceptable, so autonomous runs propose in a durable place and stop. Running as a subagent does not make a session AFK; a subagent inherits its parent's mode.

**AFK output — one proposal per placement decision**, not one per surface touched. If a lesson resolves to "this belongs in the skill's gotchas, not in memory", that is one proposal carrying both halves. File it where the routing file says (for example, a ticket in the repository that owns the target file). With no such instruction, report each proposal as a `lesson` item so the checklist carries it in the handoff as an open loop.

Each proposal must be **self-contained** — the proposed diff inline, the target file and section, and the grounding evidence (what happened, in this session, that motivates it). A proposal that only names a session id is unadjudicable later.

## Procedure

### 1. Collect grounded candidates across all surfaces

Scan the conversation once for the motivations above. Cover:

**(a) Rules you followed or fought** — the user-level rules file and every repository instruction file (`AGENTS.md`, `CLAUDE.md`) loaded this session.

**(b) Skills used** — invoked by name, loaded as guidance, or whose workflow you followed.

**(c) Skills that should have fired but didn't.** The available-skills list has been in your context all session; check the work you actually did against it. Signals, strongest first: the user pointed you at a skill mid-task ("why didn't you use /foo?") — ground truth; you reinvented a workflow a skill already encodes; a skill's domain matched but its "when to use" phrasing didn't match how the task arose.

When (c) holds and the skill genuinely applied, the fix is almost always a **description / triggering edit** — broaden or sharpen the when-to-use so it fires next time on that phrasing. Stay grounded: flag a skill only when you can point at the work *in this conversation* it was built for. Distinguish **correctly didn't fire** (leave it alone) from **trigger missed** (fix the description). If work recurred and *no* skill exists, that is "extract a new skill", out of scope here.

**(d) Agent definitions dispatched this session.** If a subagent misbehaved because its *definition* was wrong — stale path, missing constraint, an instruction that produced the wrong behavior — same grounded-edit pattern applied to that agent's definition file. An agent never dispatched this session is out of scope.

**(e) Memory.** Memories **recalled this session**, especially any that led you wrong; on an explicit full audit only, every file in the memory store this session actually uses (resolve symlinks first). For each, check the recorded fact against reality — do the files, flags, paths, commands it names still exist? Has a later decision superseded it? Verdicts: **keep** (still true, still memory-shaped) · **update** (core fact holds, details drifted) · **delete** (wrong, superseded, or duplicates another memory) · **promote** (it instructs future behavior, so place it up the routing table, then **delete the memory**; never keep the same content in two places).

If the store keeps an index file, cross-check it mechanically: every memory file gets one index line, every line points at an existing file. Orphans and dangling lines are mechanical fixes — **do them without asking**, in both modes.

### 2. Place each candidate

Run each through the routing (the routing file, or the default table).

### 3. Locate source files

Skills, agents, and rules files are often symlinked into the tool's home directory from a repository. Resolve before editing, so the change lands where it will be committed:

```bash
readlink -f <path to the installed SKILL.md>
```

A file inside an installed plugin is not yours to edit — flag it instead.

### 4. Draft to the house style

- **Explain the why.** "Do X because Y happens otherwise" is worth ten bare "ALWAYS do X" lines. Heavy all-caps MUSTs are a yellow flag — reframe as reasoning.
- **Keep it lean: pair every addition with a cut.** A long section is a signal to restructure, not to keep appending. Before proposing lines for a file, read the section you are adding to and look for what the new text makes removable: a line it supersedes or duplicates, an incident narrative that compresses to rule plus one-line why, a stale path or flag, a rule another home (a hook, a skill, the ticket) already carries. Propose those cuts in the same row. A row that adds lines and cuts none must say what you checked; leaving a file larger is a cost the row has to justify.
- **Descriptions are the trigger.** For a triggering fix, the frontmatter description is the thing to edit — specific about when to fire *and when not to*.
- **Progressive disclosure.** Large reference material belongs in a `references/` file the SKILL.md points at, not inline.
- **Rules files are aggressively concise** — match that register; they load in every session.
- Tone: written to the future agent, not to this reviewer.

### 5. HITL — present one table, then stop

One table for all surfaces. Grounding gets a candidate INTO the table; impact decides whether it should be applied. Every row carries both:

- **Impact this session** — what it actually cost: turns burned, a wrong action taken, or nothing because you caught it.
- **Impact on future sessions** — how often the moment recurs and what it costs when it does: damage (a wrong or destructive action), a lost cycle, or noise.
- **Strip / compress** — the cuts from step 4 for the same file, and the row's net line change (`+3 −5`).
- **Recommend** — apply / skip, from those two. Skip a grounded edit whose future impact is noise; every line added is context every session pays for. Say which rows you recommend and why the others are not worth their line.

```
| Target | Repo | Tier (placement) | Proposed change | Evidence (this conversation) | Impact this session | Impact on future sessions | Strip / compress | Recommend |
|---|---|---|---|---|---|---|---|---|
| build-sim skill | tools | skill (gotchas) | Note: booting an already-booted simulator exits non-zero but is harmless | We read that message as a build failure | 2 turns lost | Every re-run on a warm simulator; a lost cycle each time | Fold the two older simulator-boot notes into this one; `+1 −2` | apply |
| write-nudge hook | tools | hook | Exclude `merge-base` from the push/merge trigger | Nudge fired after a read-only ancestry check | none, ignored | Advisory noise only | none: config change, no prose; `+0` | skip |
| old-path memory | — | memory → delete | Recorded path no longer exists | `ls` failed on it | 1 turn | Misleads every recall until removed | the whole file; `−1 file` | apply |
```

**"Nothing grounded" is a normal, good outcome.** Say so plainly and stop — "skills used this session all ran cleanly; no rule friction; memories verified." Manufacturing edits to fill the table is the failure mode, not the absence of them.

**Limitation:** self-detecting under-trigger is best-effort. If you never knew a skill existed, you may not realize it at wrap-up either. The retrospective scan catches most of it; the user's mid-task corrections catch the rest.

### 6. Apply

After approval (HITL) — surgical edits only. Don't reflow unrelated content; match each file's existing heading style. Keep any memory index in step with memory files added, changed, or removed.

### 7. Leave commits to the persist step

**Do not commit or push from this skill.** Edits land as working-tree changes; report which repositories are now dirty so the checklist's `persist` step (or the user) commits them.

## Output format

```
Routing: <routing file path | default>
Surfaces scanned: rules · skills+agents (<list>) · memory (<n> recalled)
Mode: HITL | AFK

<the table from step 5 with impact, strip/compress, and recommend columns, or "nothing grounded — <one line on what was checked>">
Net: <per file touched, lines added and removed>
Recommend: apply <rows> · skip <rows> — <one line why the skips are not worth their line>

[HITL, after approval]
Applied:
- <target> (<repo>): <one-line change>
Repositories now dirty: <list>

[AFK]
Proposed: <where each was filed, or "carried as lesson items"> — <lesson>   (edited nothing)
```

## Gotchas

- **Memory is the last rung, not a parking spot.** A "memory" that instructs future behavior was misfiled at write time. Promote it and delete the original — a duplicate in two places drifts, and the memory copy is the one nobody re-reads.
- **Symlinked sources.** Editing through a symlink is fine, but resolve it before reasoning about which repository the change commits to; getting this wrong leaves the edit uncommitted in a repository you never look at.
- **Don't route a skill-local lesson into a shared rules file.** A user-level rules file loads in *every* session. A gotcha that only matters inside one skill costs every unrelated session context if it lands there.
