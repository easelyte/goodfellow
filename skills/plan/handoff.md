## 4. Self-review

- Grep clean: no placeholders, every code step has concrete content
- Spec-coverage: every spec section has at least one plan task
- Internal consistency: dependency graph matches task bodies

## 5. Auto-dispatch review

After writing + self-review, in the same turn:
1. Emit summary (file path, task count, integration risks)
2. Dispatch `/goodfellow:review --plan <plan-path>`

No gate. The operator reviews through review, not by approving the plan directly.

**Execution footer:**
> Use `/goodfellow:execute <plan-path>` to implement this plan.
