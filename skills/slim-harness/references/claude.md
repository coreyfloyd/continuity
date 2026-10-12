# Claude Code limits and loading

Checked 2026-10-12. Measure only the Claude Code session's loaded files.
Auto-loaded rules/output styles and skills or agent definitions read in full are
in context; a listed-only skill gets description checks only. The parent resolves
every role path the dispatched definition requires and measures those files itself.

| File | Limit | Unit |
|---|---:|---|
| CLAUDE.md and rules | 200 | lines |
| SKILL.md body (exclude frontmatter) | 500 | lines |
| Skill description | 1,024 | characters |
| Skill description plus when_to_use | 1,536 | characters |
| agents/*.md and each required role file | 200 | lines |
| Output style | 200 | lines |

Report words and UTF-8 bytes alongside line counts. A file equal to a limit is
under it; only strictly greater sizes produce a trigger row. The 200-line policy
for agents, roles and styles is continuity's chosen review threshold, not a
vendor truncation limit.

Situational material applying to one codebase area can move into `.claude/rules/`
with `paths:` YAML frontmatter matching that area's files. Offer this destination
alongside a pointer; path-scoped rules load when matching files are touched and
need no pointer. For tool-specific material, use an entry only Claude Code reads.

Sources (checked 2026-10-12):

- [Claude Code memory](https://code.claude.com/docs/en/memory): keep CLAUDE.md concise;
  200-line guidance, path-scoped rules and layered instruction behavior.
- [Claude Code skills](https://code.claude.com/docs/en/skills): discovery text combines
  description and when_to_use, capped at 1,536 characters in the listing.
- [Agent Skills specification](https://agentskills.io/specification): 1,024-character
  description maximum, shared with Codex.
- [Skill best practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices):
  500-line body guidance, one-level references and TOCs above 100 lines for skills.
- [Context engineering](https://claude.com/blog/the-new-rules-of-context-engineering-for-claude-5-generation-models):
  redundancy, overconstraint, examples and rewriting rules as judgment criteria.

The extension of pointer depth and TOC requirements to rules files is this skill's
own policy; the published best-practices page makes those claims for skills only.
