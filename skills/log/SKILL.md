---
name: log
description: Personal work log for the current git repo, built from your Claude Code chats and your own commits (features, bug fixes, env/production debugging for other teams, investigations, tooling). Use for "/wlog:log", "log this", "what did I do", "scan my chats", "work log pdf", standup, probation or performance review prep.
argument-hint: "help | list [week|month|YYYY-MM] | add <what you did> | scan | pdf [week|month|quarter|all|YYYY-MM-DD YYYY-MM-DD] [--full] | artifact [update] [same periods] | setup"
---

# Work log

Script: `scripts/worklog.py` in this skill's folder (the base directory shown when the skill loads). Call it as `python3 -I "<base dir>/scripts/worklog.py" <command>`, always with the repo as the current folder. Every command is per repo: data lives in `~/.claude/worklog/<repo>/` (`config.json`, `entries.jsonl`, `commits.jsonl`, `scan-state.json`, `digests/`). Never edit `entries.jsonl` by hand except to fix an entry the user asks about.

If a command says "No work log for <repo> yet", run `setup` first, then carry on with what the user asked.

## Commands

- `help`, `--help`, `-h`, or an argument you don't recognise: print the card below exactly as written, run nothing.

```
/wlog:log                        what you did in the last 2 weeks
/wlog:log list month             same, for a period (week, month, quarter, year, all, 2026-10, or two dates)
/wlog:log add <what you did>     log something by hand (meetings, console work, anything outside Claude)
/wlog:log scan                   find work in your chats and commits that is not logged yet
/wlog:log pdf                    1-page summary PDF + interactive page, in Downloads (all time)
/wlog:log pdf month              same, for the last 30 days
/wlog:log pdf 2026-10-01 2026-10-31   same, for exact dates (YYYY-MM-DD)
/wlog:log pdf --full             summary + one-line list of everything
/wlog:log artifact [period]      new online page with a new link
/wlog:log artifact update [period]   refresh your last online page, same link
/wlog:log setup                  first-time setup for this repo
/wlog:log help                   show this card

Periods: week · month · quarter · year · all · YYYY-MM · FROM [TO] as YYYY-MM-DD
Hide an item or change highlights: just ask, e.g. "hide the lint fix from my work log"
Regroup: just ask, e.g. "put all notification work under one Notifications project"
After setup, work is logged at the end of each task and your commits are recorded automatically.
```

- `setup`: first time in a repo (or on a new machine).
  1. `worklog.py init`: detects name and email from git config, the first commit date, GitHub from `origin`, and the Jira site from the chats. Show what it found. If Jira was not found and the user uses Jira, ask for the site and run `worklog.py set jira https://<site>.atlassian.net/browse/`.
  2. If this skill's base directory is inside `~/.claude/plugins/`, it was installed as a plugin: the plugin already provides the commit hook and the logging rule, so skip this step. Otherwise (standalone skill), run `worklog.py install`: it adds the commit hook to `~/.claude/settings.json` (backup kept) and the logging rule to `~/.claude/CLAUDE.md`. Idempotent. Tell the user to restart Claude Code once.
  3. Offer `scan` to backfill past work.
- no argument or `list [period]`: `worklog.py list [period]` (default: last 14 days). Show it grouped by project, then Features / Bug fixes / Miscellaneous, short.
- regroup ("put X under Y", "group my notification work"): find the ids with `worklog.py list all`, then `worklog.py project "<name>" <id> <id> ...`. If several entries are small steps of the same work, offer to merge them into one entry (add the merged entry, hide the old ones).
- `add <description>`: build one entry from the description and the current chat, then `worklog.py add '<json>'`.
- `scan`: catch up from chat history and commits.
  1. `worklog.py commits` (imports the user's own commits since `since` in config; safe to repeat).
  2. `worklog.py extract` (only chats and messages not scanned yet; `--all` rescans everything). It prints the `batch-N.txt` files it wrote (~15k tokens each).
  3. Do not read the batch files yourself. Run `worklog.py list all > <scratchpad>/logged.txt` and `worklog.py projects`, then start one subagent per batch file, all in one message, with `model: "haiku"`. Give each: the batch path, the logged.txt path, the project names, the entry fields and skip rules from "Entry format" below, and the date range of `commits.jsonl`. Each subagent reads only its batch and logged.txt and returns a JSON array of new entries (no file writes; `[]` if nothing new). Haiku is enough for this; it costs a fraction of the main model.
  4. Merge the arrays, drop duplicates (same work across batches, or already in logged.txt), show the entries briefly, add them, then `worklog.py mark-scanned`.
- `pdf [period] [--full]`: `worklog.py pdf [period] [--full]` → `~/Downloads/worklog-<repo>.html` (interactive, with PDF buttons) + `~/Downloads/worklog-<repo>-<from>-to-<to>-summary.pdf` (1 page; `--full` = summary + one-line list). Default period: all. Give the user the paths. Do not publish anything.
- `artifact [period]`: always a **new** page with a new link. Run `worklog.py pdf [period]` (no period = all work), copy the "online page file" it prints to a new file named `worklog-<repo>-<from>-to-<to>-<HHMMSS now>.html` in the scratchpad (a path never used before, so it becomes a new artifact), and publish that copy with the Artifact tool without `url` (icon `list`). Save the link with `worklog.py set artifact_url <link>`. Give the user only the link, and say it is private until they share it.
- `artifact update [period]`: refresh the **last** page, same link. Run `worklog.py pdf [period]` (no period = all work; a period = only that range), then publish the "online page file" with `url` = `worklog.py get artifact_url`. If there is no saved link, or the publish says the artifact no longer exists, tell the user and make a new one as in `artifact` instead.

## Entry

One entry = one piece of work the user could mention in a review. Add with `worklog.py add '<json>'` (duplicates by date + title are ignored; in a repo without a work log it does nothing).

```json
{"date": "2026-10-26", "end": "", "type": "debugging", "title": "Found why restaurant setup was stuck on main",
 "project": "Restaurant setup", "ticket": "", "what": "...", "why": "...", "outcome": "...", "evidence": ["told DevOps", "commit 5d84502"],
 "env": "main", "handed_to": "DevOps", "status": "done", "source": "<chat id or commit>"}
```

- `type`: `feature` (ticketed feature work), `fix` (bug fix or improvement, any ticket or none), `debugging` (finding the cause of an env/production problem, often owned by someone else, handed to DevOps or another team), `investigation` (analysis or report for the lead), `tooling` (scripts, skills, shared setup, helping teammates).
- `project`: the feature area the work belongs to, in 1-4 words ("Notifications", "Offboard restaurant", "Dev environment"). The report groups by project first, then Features / Bug fixes / Miscellaneous (debugging, investigations, tooling). Before adding, run `worklog.py projects` and reuse an existing project name exactly; only start a new one for a new area. Never put the ticket id or the type in the name.
- `title`: short, review-friendly, outcome first ("Fixed X", "Found why Y").
- `what` / `why` / `outcome`: one or two plain sentences each. Say the impact (who was blocked, what now works).
- `evidence`: commits, PRs, ticket ids, who was told. Never secrets, tokens, env values or customer data.
- Only the user's own impactful work. Skip merges, rebases, backsyncs, conflict or build fixes caused by someone else's PR, test data seeding, personal local setup, self code-review passes and one-line tweaks.
- **One entry per feature.** Everything about a feature (sub-tasks, follow-up fixes, reviews, docs, tests, feature-specific investigations) lives in that feature's one entry with a date range (`end`). When new work touches a feature already logged, add a merged entry with the updated text and range and hide the old one; never a new line per sub-task. A few months of work is a few features, not dozens. Separate entries only for env/production debugging, reports for the lead, tooling, and unrelated small fixes.

## Report config (`~/.claude/worklog/<repo>/config.json`)

- `hidden`: entry ids left out of the report (data stays). To hide or restore an item, find its id with `worklog.py list all` and edit this list.
- `highlights`: up to 10 entry ids shown as highlights. Empty = picked automatically (newest features first).
- `projects`: `{ "TICKET-1": "name" }`: project name for entries that have no `project` of their own, by ticket. Entries with neither land under the ticket id or "Other work". Projects with 2+ entries show as chips.
- `name`, `email`, `since`, `jira`, `github`, `artifact_url`: change with `worklog.py set KEY VALUE`.

## Notes

- Chat history only covers work done with Claude. Ask the user to `add` meetings, reviews or console work done outside Claude.
- The hook records only the user's own commits (matched by email), and only in repos that have a work log.
- PDFs need Chrome, Chromium or Edge. Without one, the HTML page has PDF buttons that use the browser's print dialog.
