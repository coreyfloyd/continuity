# Session Continuity Glossary

Defines the twelve terms the session-continuity package uses. Other glossaries that mention these terms point here instead of defining them. "Artifact" is a session-item type (see **Open loop**), not a term of its own.

## Language

**Interactive session**:
A session a person starts and can keep prompting after it begins, or one that another interactive session starts on that person's behalf. Interactive sessions write and read handoffs. A session started by an automated scheduler, and a subagent, are not interactive sessions and never write or read one.
_Avoid_: chat, one-shot session

**Record**:
The durable place a session's work is kept: tickets, project status, documents, and their history. It is the primary persistence, and a handoff only points to it. The package names the record but does not provide one; a profile binds the `record` slot to whatever keeps it.
_Avoid_: handoff, session memory

**Handoff**:
The thin pointer one interactive session writes for one subject, so the next session can find its way back to the record. A handoff is identified by its subject and its session together, so two sessions on the same subject never overwrite each other. It holds where to find the record, the session's open loops, and the uncommitted files the session changed. It holds nothing the record holds. Checkpoint and wrap both end by writing it.
_Avoid_: summary, record

**Subject**:
What a handoff is about: the working directory of an ordinary session, or the channel of a session that answers one channel.
_Avoid_: session, repository

**Sibling**:
A handoff whose subject is a different worktree of the same repository as the current subject. Resume lists siblings and marks them as such.
_Avoid_: duplicate, related session

**Checkpoint**:
Writing the handoff now, without finishing the session's work. A checkpoint never commits, merges, or pushes. It carries every session item the record does not yet hold as an open loop. When the handoff fails its validity check, a checkpoint still writes it and reports the failure.
_Avoid_: wrap, save

**Wrap**:
Finishing a session: running the checklist, which is the spine plus any profile steps, and ending by writing and validating the handoff. Unlike a checkpoint, a wrap fails, and says why, when the handoff fails its validity check.
_Avoid_: checkpoint

**Spine**:
The fixed five steps every wrap runs, in this order: reconcile the record, capture knowledge, fold in lessons, persist, write and validate the handoff. A profile can add steps around the spine but cannot remove or skip one of its steps. Only one step runs after the last: `own-worktree`, which removes the session's own worktree once the handoff is written.
_Avoid_: checklist (the checklist is the spine plus profile steps)

**Profile**:
One file that extends the checklist: it binds slots, adds steps before the spine, and adds steps after any of the spine's first four steps, and may add the one step `own-worktree` after the handoff. A user profile applies to every session; a repository profile adds steps for sessions in that repository.
_Avoid_: configuration (the configuration is the one value naming where handoffs are stored)

**Slot**:
A named spine step that a profile binds to a command. There are four: `record`, `knowledge`, `lessons`, and `persist`. A binding names the session-item types its slot consumes, and in a resumed session the slot also consumes the carried open loops of those types. A slot with nothing bound does not fail the checklist: the items it would have consumed are carried in the handoff as open loops. The handoff step is not a slot.
_Avoid_: hook, plugin

**Ledger**:
The per-session list of what one session touched: one entry for each file it changed and each artifact it published. Checkpoint reads the ledger to list uncommitted files and pending artifacts, so neither depends on the session remembering them. A publishing tool records an artifact through the package's one ledger command.
_Avoid_: log, history

**Open loop**:
One item carried in a handoff, with a disposition saying what should happen to it. A carried item names its session-item type (progress, commitment, decision, knowledge, lesson, artifact, or open loop) and keeps the identity of the session that produced it and when; a decision also keeps who made it and where. A slot that consumes an open loop removes it from the handoff.
_Avoid_: todo, task
