# Using Goodfellow with Matron

Goodfellow works on its own and keeps its follow-up loops in `.goodfellow/loops.json`. If you already run a [Matron](https://github.com/Matronhq) journal, it is one option for where those loops live: they then show up as tracker items next to your other work. Nothing changes until you set the variables below.

## What you need

- A Matron journal your machine can reach over HTTPS (plain HTTP is accepted only for a loopback address such as `127.0.0.1`).
- An agent token for that journal, saved in a file only you can read (`chmod 600`; a file open to its group or to others is refused).
- A conversation on the journal for goodfellow to file items on.

## Configure

Set these in the environment of the Claude Code session:

| Variable | Required | Meaning |
|---|---|---|
| `GOODFELLOW_MATRON_URL` | yes | Journal base URL, for example `https://journal.example.com`. No credentials, query or fragment. |
| `GOODFELLOW_MATRON_TOKEN_FILE` | yes | Absolute path of the file holding the agent token. The token is never read from an environment value, printed, logged or written under `.goodfellow/`. |
| `GOODFELLOW_MATRON_CONVO` | yes | Id of the conversation items are filed on. |
| `GOODFELLOW_MATRON_MISSION` | no | A mission number (`7` or `#7`) or id. When set, closing a loop posts a progress milestone to it. |

If any of these is set, goodfellow uses the journal. A missing required variable, an unknown `GOODFELLOW_MATRON_*` name (a typo, say), a bad URL, or a token file that other users can read stops the command with an error. It never quietly falls back to the local file, so you cannot file loops in the wrong place by accident. Unset all of them to go back to the local store.

## What gets filed

Each loop becomes a task item on the conversation you named. The loop number you see in goodfellow is the item number.

| Goodfellow field | On the item |
|---|---|
| title | title |
| description | body text |
| priority `p1` to `p4` | label `p1` to `p4` |
| tags | labels `gf:<tag>` (letters, digits and `_ . : / -`, at most 37 characters) |
| (marker) | label `goodfellow` |
| (identity) | label `gf-uuid:<hex>`, used to find the item again if a response is lost |
| owner, source, next action, lens, triage count, last triaged | a fenced `work-meta` JSON block at the end of the body |

Goodfellow only ever lists, reads, edits or closes items that carry the `goodfellow` label and were filed on your conversation. Everything else on the journal is invisible to it. Editing a goodfellow item by hand is fine; if you damage the `work-meta` block, reads fall back to defaults and writes to that item refuse rather than guess.

Commands (`loop_store.py add|close|get|list|stale|count|update-triage`; `get <id>` prints one loop as JSON, including its `uuid`) behave the same as with the local file. `close` resolves the item as done. With `GOODFELLOW_MATRON_MISSION` set it also posts a milestone titled "Closed: <title>" to that mission. Goodfellow never closes a mission. If the milestone cannot be posted (the mission is unknown or not linked to your conversation) the item stays closed and the command fails with a message saying so; run `close` again once the mission is fixed and the milestone is posted (once).

Updates to `triage_count` and `last_triaged` re-read the item immediately before writing, give up and redo on any change, and check the result afterwards. The journal offers no revision check on edits, so a person saving an edit to the same item in the same instant as a goodfellow write can still be overwritten; the window is a few milliseconds.

## Start-up check

Before its first request, goodfellow runs a read-only capability probe against the journal and stops with an error naming the first capability that is missing. It checks exactly what the backend relies on:

- The journal's own `GET /help` documents the routes and fields goodfellow uses: `GET /items` with `convo` and `label` filters and `{items, next_cursor}` paging, `GET /items/:id`, `POST /items` with an `Idempotency-Key`, `PATCH /items/:id` with `body`, `POST /items/:id/close` with a `resolution`, and `POST /milestones` with an `Idempotency-Key`.
- `GET /items` answers in that shape (checked with `limit=1`, once filtered to your conversation and once unfiltered): each item has `id`, `num`, `state` (`open` or `closed`), `labels`, `awaiting`, `title`, `body`, `origin_convo_id`, and a `created_at` in epoch milliseconds; the `label` and `convo` filters are honoured.

The probe sends only `GET` requests. A journal that passes needs nothing else, so a missing version announcement is fine. If the journal does announce an API version (response header `X-Journal-Api-Version` or a `Journal API version: <n>` line in `GET /help`), goodfellow supports version `1` and refuses any other, newer or older, before sending anything else.

The only requests goodfellow sends are `GET /help`, `GET`/`POST /items`, `GET`/`PATCH /items/#<n>`, `POST /items/#<n>/close` and `POST /milestones`. Redirects are never followed, so the token is only ever sent to the URL you configured.

## The stop list

Unchanged. Force pushes, pushes to the default branch, releases and package publishes still ask for confirmation, and in a Matron session that confirmation reaches you the way any other permission request does.

## Testing against your journal

The contract tests run against recorded response shapes and need no journal. To run the live round trip too (it files one item and closes it):

```sh
GOODFELLOW_MATRON_LIVE_TEST=1 GOODFELLOW_MATRON_URL=... GOODFELLOW_MATRON_TOKEN_FILE=... \
  GOODFELLOW_MATRON_CONVO=... python -m pytest scripts/test_backend_matron.py -k live
```
