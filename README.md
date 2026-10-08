# continuity

Session continuity for coding agents. A session that ends, compacts, or runs out of
context leaves a thin handoff that points the next session back to the record, and a
wrap-up checklist saves the session's work before it ends. The same engine, skills, and
storage work for Claude Code and Codex.

This repository is the shared package. It holds the parts neither tool owns:

| Part | Where |
|---|---|
| Handoff engine: resolve, checkpoint, lint, ledger, hook decisions | `scripts/lib/handoff.py` (standard library only) |
| Checklist engine: spine, profiles, slots, carried items, wrap check | `scripts/lib/checklist.py` (standard library only) |
| Six skills: `resume-checkpoint`, `resume-work`, `checklist`, `handoff-prompt`, `wip`, `harness-improve` | `skills/` |
| Six hook adapters, each a few lines over the engines | `scripts/` and `hooks/` |
| Glossary of the twelve terms | `docs/glossary.md` |
| Storage, configuration, and profile contracts | `INTERFACE.md` |
| Tests | `tests/` |

Adapters install this package into one tool each: a Claude Code plugin and a Codex install
script. They register the hooks and place the skills. They are separate from this
repository's engine and build against the interface in `INTERFACE.md`.

## What you get

- **Checkpoint.** Write the handoff now, without finishing. It carries every session item
  the record does not yet hold as an open loop, and it never commits, merges, or pushes.
- **Wrap.** Run the checklist: reconcile the record, capture knowledge, fold in lessons,
  persist, then write and validate the handoff. A profile binds the first four steps to
  your own commands and adds steps of its own. It cannot remove or skip a spine step.
- **Resume.** List the handoffs for the current directory, newest first, and pick one up in
  either tool. With no handoff, fall back to recent sessions from the tool's own history.
- **Hooks.** Restore the handoff after compaction, back up the hook input before it, warn
  when half the context window is used, record the files and artifacts a session touches,
  flag a stale handoff at stop, and remind once to run the checklist after a commit, a push,
  or a passing test run.

With nothing configured the package works: handoffs go to
`~/.local/state/continuity/handoffs`, outside both tools' own data areas, so uninstalling
an adapter leaves them in place.

## Requirements

Python 3.9 or newer, standard library only. Bash 3.2 or newer. `git`. `gh` only if you use
the commitments scan in `resume-work`.

## Tests

```bash
python3 -m pip install pytest
python3 -m pytest tests
bash tests/test-resume-work-wrapper.sh
bash tests/test-resume-work-branch-scan.sh
```

The fixtures in `tests/fixtures` are records captured from real sessions, with names,
repositories, projects, products, channels, paths, and addresses replaced by neutral
stand-ins. `tests/fixtures/resume-work-codex-PROVENANCE.md`
says how.

### Keeping a setup out of the repository

Nothing here may name its maintainer, or the maintainer's machines, repositories,
projects, products, paths, or channels. The list
of terms that check looks for stays outside the repository, so the check ships no reference
itself. Point it at your list and run it over the tree:

```bash
REFERENCE_CHECK_TERMS=/path/to/terms.txt python3 -m pytest tests/test_shipped_surface.py
python3 scripts/lib/reference_check.py --terms /path/to/terms.txt .
```

One term per line, matched case-insensitively. A separator (`-`, `_`, `.`, or a space)
between two letters or digits of a term matches any run of separators or none, so
`example-host` also catches `Example Host` and `examplehost`. Lines starting with `#` are
comments; write `\#` to start a term with `#`. Exit status 0 is clean.

## License

Apache-2.0. See `LICENSE`.
