---
name: branch
description: Create an isolated git worktree for feature work — clean separation between feature development and main branch.
---

Create a worktree for: $ARGUMENTS

## 1. Derive branch name

From the topic argument, create a kebab-case branch name:
- `shipline-<topic>` prefix
- Max 50 characters
- Strip special characters

Example: `/shipline:branch auth-refactor` → branch `shipline-auth-refactor`

## 2. Create worktree

```bash
git worktree add -b "<branch-name>" "../<branch-name>" HEAD
```

If the branch already exists, use it:
```bash
git worktree add "../<branch-name>" "<branch-name>"
```

## 3. Report

"Worktree created at `../<branch-name>`. Open a new Claude Code session there to begin work."

Return the worktree path for use by execute.
