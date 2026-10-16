## 0. Initialize project state

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/init_state.sh"
```

## 1. Classify the tier

Pick the tier from this rubric. The first matching row from the top wins; **when unsure, pick the
higher tier.** A tier can be raised later, never lowered.

| Tier | Pick when |
|---|---|
| **T3 live** | The change mutates live state: migrations or production data, deploy/cron/services, credentials, deleting or backing up data, money, messages to real people. |
| **T2 design** | A new concept, data model or cross-component contract; ambiguous intent; or two or more reasonable designs. |
| **T1 feature** | A new, bounded, reversible behaviour inside the existing model. |
| **T0 fix** | Existing behaviour is wrong and can be reproduced. |

Then let the resolver apply the hard floors (a high-stakes path forces at least T1; a live-state path
or trigger forces T3) and any `--tier` the operator gave:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/tier.py" resolve --paths <files you expect to touch> \
  --proposed <T0..T3> [--tier <operator's --tier>] [--live-state "<what live state it touches>"] \
  --reason "<one line: the rubric row that decided it>"
```

- **Exit 0:** print its two lines as the announcement (`Tier T1 (feature): …` / `Floor …`) and go on.
- **Exit 3:** the operator's `--tier` is below a hard floor. Say so with the resolver's reason and
  continue at the floor tier. Never honour it silently.
- **Exit 2:** the resolver could not decide (for example a configured path list is missing). Stop
  and report it; never fall back to T0.

Pass `--live-state` whenever the T3 row matched on intent rather than on a path, so the operator
cannot lower it by accident.

