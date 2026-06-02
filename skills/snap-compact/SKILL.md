---
name: snap-compact
description: Extract learnings from the session before context compaction — preserves knowledge that would otherwise be lost when the context window is compressed.
---

Extract learnings before compacting context.

## 1. Scan for learnings

Review the current session's work for:
- **Principles:** design rules that emerged
- **Patterns:** solutions that worked well
- **Gotchas:** footguns or surprising behaviors discovered

Focus on learnings from the current session that haven't been captured yet.

## 2. Persist to knowledge file

Append any found learnings to `.shipline/knowledge.md` with `[pending]` tag and date:

```
- [pending] 2026-06-02: <learning text>
```

If `.shipline/knowledge.md` doesn't exist, create it with the three section headers:

```markdown
## Principles

## Patterns

## Gotchas
```

Then append entries to the appropriate section.

## 3. Compact

Proceed with context compaction. The learnings are now persisted and will survive the context loss.

Report: "Extracted N learnings to .shipline/knowledge.md before compacting."
