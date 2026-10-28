#!/usr/bin/env python3
"""Personal work log for any git repo, built from your Claude Code chats and your commits.
Run it from inside the repo. Data: ~/.claude/worklog/<repo>/ (config.json, entries.jsonl, commits.jsonl, scan-state.json).

  worklog.py init                  start a work log for this repo (detects your name, email, GitHub, Jira)
  worklog.py install               standalone skill only: add the commit hook and logging rule to ~/.claude (the plugin ships its own hooks)
  worklog.py status                is this repo set up, how many entries, what is configured
  worklog.py add '<json>'          append one entry (fields below); prints its id
  worklog.py list [PERIOD]         entries, one line each with id and project (default: last 14 days)
  worklog.py projects              project names in use, with entry counts
  worklog.py project NAME ID...    put entries under one project (e.g. "Notifications")
  worklog.py commits [--since DATE]   import your commits from git into commits.jsonl
  worklog.py extract [--all]       write compact digests of chats not scanned yet to the digest folder
  worklog.py mark-scanned          after entries from the digests are added, remember them as scanned
  worklog.py pdf [PERIOD] [--full] interactive ~/Downloads/worklog-<repo>.html + 1-page summary PDF (--full adds the list)
  worklog.py set KEY VALUE         change a config value (name, jira, github, since, artifact_url)
  worklog.py get KEY               print a config value
  worklog.py commit-hook           PostToolUse hook: records the latest commit after a `git commit`
  worklog.py session-context       SessionStart hook: prints the logging rule in repos that have a work log

PERIOD: week | month | quarter | year | all | YYYY-MM | FROM [TO] (dates as YYYY-MM-DD). Default for pdf: all.

Entry fields: date (YYYY-MM-DD), end (optional), type (feature|fix|debugging|investigation|tooling), title,
project (feature area, e.g. "Notifications"), ticket (e.g. ABC-123, or ""), what, why, outcome, evidence (list: commits, PRs, who was told), env (optional),
handed_to (optional), status (done|pending|handed-off), source (chat id or "commit").
"""
import collections
import datetime as dt
import glob
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys

HOME = os.path.expanduser("~")
CLAUDE = os.path.join(HOME, ".claude")
BASE = os.path.join(CLAUDE, "worklog")
TYPES = {
    "feature": "Features",
    "fix": "Bug fixes & improvements",
    "debugging": "Production / environment debugging",
    "investigation": "Investigations & reports",
    "tooling": "Tooling & team help",
}
TICKET = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d{1,6}\b")
SECRET = re.compile(r"(eyJ[\w-]{20,}\.[\w-]+\.[\w-]+|(?i:bearer)\s+\S+|AKIA[0-9A-Z]{16}|-----BEGIN[^-]+-----[\s\S]*?-----END[^-]+-----)")
OTHER = "Other work"
NOISE = re.compile(r"^(merge|backsync|index on|revert \"merge)|conflict|yarn\.lock|package-lock|lint issue", re.I)


def git(*args, cwd=None):
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True, cwd=cwd).stdout.strip()
    except (FileNotFoundError, NotADirectoryError):
        return ""


def repo_root(cwd=None):
    """Main checkout of the repo at cwd, so worktrees share one log."""
    common = git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=cwd)
    if not common:
        return ""
    return os.path.dirname(common) if os.path.basename(common) == ".git" else git("rev-parse", "--show-toplevel", cwd=cwd)


class Repo:
    def __init__(self, cwd=None):
        self.root = repo_root(cwd)
        self.name = os.path.basename(self.root) if self.root else ""
        self.data = os.path.join(BASE, self.name) if self.name else ""
        self.entries = os.path.join(self.data, "entries.jsonl")
        self.commits = os.path.join(self.data, "commits.jsonl")
        self.state = os.path.join(self.data, "scan-state.json")
        self.digests = os.path.join(self.data, "digests")
        self.config_path = os.path.join(self.data, "config.json")

    @property
    def ready(self):
        return bool(self.data) and os.path.exists(self.config_path)

    def need(self):
        if not self.root:
            sys.exit("Not inside a git repo. cd into the repo you want a work log for.")
        if not self.ready:
            sys.exit(f"No work log for {self.name} yet. Run: worklog.py init")
        return self

    @property
    def config(self):
        return json.load(open(self.config_path)) if os.path.exists(self.config_path) else {}

    def save_config(self, cfg):
        json.dump(cfg, open(self.config_path, "w"), indent=2)

    def chat_glob(self):
        # Claude Code stores chats in ~/.claude/projects/<path with every non-alphanumeric char as '-'>; worktrees add a suffix
        return os.path.join(CLAUDE, "projects", re.sub(r"[^A-Za-z0-9]", "-", self.root) + "*", "*.jsonl")


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    out = []
    for line in open(path, encoding="utf-8", errors="ignore"):
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def append_jsonl(path, obj):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def period(args, default="all", first=None):
    """Turn PERIOD args into (start, end) ISO dates."""
    today = dt.date.today()
    args = [a for a in args if not a.startswith("--")] or [default]
    back = {"week": 7, "month": 30, "quarter": 91, "year": 365}
    a = args[0].lower()
    if a in back:
        return (today - dt.timedelta(days=back[a])).isoformat(), today.isoformat()
    if a == "all":
        return first or (today - dt.timedelta(days=365)).isoformat(), today.isoformat()
    if re.fullmatch(r"\d{4}-\d{2}", a):
        y, m = map(int, a.split("-"))
        nxt = dt.date(y + m // 12, m % 12 + 1, 1)
        return f"{a}-01", (nxt - dt.timedelta(days=1)).isoformat()
    try:
        start = dt.date.fromisoformat(a).isoformat()
        end = dt.date.fromisoformat(args[1]).isoformat() if len(args) > 1 else today.isoformat()
    except ValueError:
        sys.exit(f"Bad period '{' '.join(args)}'. Use week, month, quarter, year, all, YYYY-MM, or FROM [TO] as YYYY-MM-DD.")
    return start, end


# ---------- setup ----------

def detect_jira(repo):
    """Most common https://<site>.atlassian.net in this repo's chats."""
    hits = collections.Counter()
    for path in glob.glob(repo.chat_glob())[:200]:
        with open(path, encoding="utf-8", errors="ignore") as f:
            hits.update(re.findall(r"https://([a-z0-9-]+)\.atlassian\.net", f.read()))
    hits.pop("site", None)
    hits.pop("your-domain", None)
    return f"https://{hits.most_common(1)[0][0]}.atlassian.net/browse/" if hits else ""


def detect_github(root):
    url = git("remote", "get-url", "origin", cwd=root)
    m = re.match(r"(?:git@|ssh://git@|https://)([^/:]+)[/:](.+?)(?:\.git)?/?$", url)
    return f"https://{m.group(1)}/{m.group(2)}/commit/" if m and "github" in m.group(1) else ""


def init():
    repo = Repo()
    if not repo.root:
        sys.exit("Not inside a git repo. cd into the repo you want a work log for.")
    os.makedirs(repo.data, exist_ok=True)
    cfg = repo.config
    email = git("config", "user.email", cwd=repo.root)
    first = git("log", "--all", f"--author={email}", "--reverse", "--format=%ad", "--date=short", cwd=repo.root).split("\n")[0] if email else ""
    defaults = {"name": git("config", "user.name", cwd=repo.root), "email": email,
                "since": first or (dt.date.today() - dt.timedelta(days=90)).isoformat(),
                "jira": detect_jira(repo), "github": detect_github(repo.root), "artifact_url": "",
                "highlights": [], "projects": {}, "hidden": []}
    for k, v in defaults.items():
        cfg.setdefault(k, v)
    repo.save_config(cfg)
    print(f"Work log ready for {repo.name} in {repo.data}")
    for k in ("name", "since", "jira", "github"):
        print(f"  {k}: {cfg[k] or '(not found, set with: worklog.py set ' + k + ' <value>)'}")


RULE = ("After finishing any piece of work worth mentioning in a performance review (feature step, bug fix, env/production debugging "
        "for another team, investigation or report for the lead, tooling or help for teammates), log it from the repo folder: "
        "`python3 -I \"{me}\" add '<json>'` with fields date, type (feature|fix|debugging|investigation|tooling), title, project, ticket, "
        "what, why, outcome, evidence (list), status (done|pending|handed-off); no need to read SKILL.md. It does nothing in repos without a work log. "
        "Log ticketed and unticketed work alike. Set `project` to the feature area (reuse a name from `python3 -I \"{me}\" projects`, e.g. \"Notifications\"). "
        "One entry per feature: sub-tasks, follow-up fixes, reviews, docs and tests of a feature go into that feature's existing entry (add a merged entry with the new text and date range, hide the old one), never a new line each. Plain words, what / why / outcome, no secrets. "
        "Do it at the end of the task and say so in one short line (\"Logged to work log.\"). Skip trivial questions and explanations. "
        "Log only the user's own impactful work: skip merges, rebases, backsyncs, conflict or build fixes caused by someone else's PR, "
        "test data seeding, personal local setup, self code-review passes and one-line tweaks.")


def session_context():
    # SessionStart hook (plugin): give Claude the logging rule, only in repos that have a work log. Prints nothing otherwise.
    try:
        cwd = json.load(sys.stdin).get("cwd") or os.getcwd()
    except Exception:
        cwd = os.getcwd()
    repo = Repo(cwd)
    if repo.ready:
        print(f"Work log is on for {repo.name}. " + RULE.format(me=os.path.abspath(__file__)))


def install():
    """Add the commit hook to ~/.claude/settings.json and the logging rule to ~/.claude/CLAUDE.md. Idempotent."""
    me = os.path.abspath(__file__)
    hook_cmd = f'python3 -I "{me}" commit-hook 2>/dev/null || true'
    os.makedirs(CLAUDE, exist_ok=True)
    settings_path = os.path.join(CLAUDE, "settings.json")
    settings = json.load(open(settings_path)) if os.path.exists(settings_path) else {}
    post = settings.setdefault("hooks", {}).setdefault("PostToolUse", [])
    ours = [h for g in post for h in g.get("hooks", []) if "worklog" in h.get("command", "") and "commit-hook" in h.get("command", "")]
    if any(h["command"] == hook_cmd for h in ours):
        print("commit hook: already installed")
    elif ours:  # plugin moved (update/reinstall): point the existing hook at this copy
        shutil.copy(settings_path, settings_path + ".bak-worklog")
        for h in ours:
            h["command"] = hook_cmd
        json.dump(settings, open(settings_path, "w"), indent=2)
        print(f"commit hook: updated to {me}")
    else:
        if os.path.exists(settings_path):
            shutil.copy(settings_path, settings_path + ".bak-worklog")
        post.append({"matcher": "Bash", "hooks": [{"type": "command", "command": hook_cmd, "if": "Bash(git *)"}]})
        json.dump(settings, open(settings_path, "w"), indent=2)
        print(f"commit hook: added to {settings_path} (backup: settings.json.bak-worklog)")
    rule_path = os.path.join(CLAUDE, "CLAUDE.md")
    rule = open(rule_path).read() if os.path.exists(rule_path) else ""
    if "## Work log (always on)" in rule:
        fixed = re.sub(r'python3 -I "?[^"`\s]*worklog\.py"?', lambda m: f'python3 -I "{me}"', rule)
        if fixed != rule and f'python3 -I "{me}"' not in rule:
            open(rule_path, "w").write(fixed)
            print("logging rule: path updated in CLAUDE.md")
        else:
            print("logging rule: already in CLAUDE.md")
    else:
        with open(rule_path, "a") as f:
            f.write("\n## Work log (always on)\n\n" + RULE.format(me=me) + "\n")
        print(f"logging rule: added to {rule_path}")
    print("Restart Claude Code (or open /hooks once) so the hook loads.")


def status():
    repo = Repo()
    if not repo.root:
        sys.exit("Not inside a git repo.")
    if not repo.ready:
        print(f"{repo.name}: no work log yet. Run: worklog.py init")
        return
    cfg = repo.config
    entries, commits = read_jsonl(repo.entries), read_jsonl(repo.commits)
    print(f"{repo.name}: {len(entries)} entries ({len(cfg.get('hidden', []))} hidden), {len(commits)} commits, "
          f"{len(json.load(open(repo.state))) if os.path.exists(repo.state) else 0} chats scanned, "
          f"{len(glob.glob(repo.chat_glob()))} chats on disk")
    for k in ("name", "since", "jira", "github", "artifact_url"):
        print(f"  {k}: {cfg.get(k) or '-'}")


def set_key(key, value):
    repo = Repo().need()
    if key not in ("name", "email", "since", "jira", "github", "artifact_url"):
        sys.exit("key must be one of: name, email, since, jira, github, artifact_url")
    cfg = repo.config
    cfg[key] = value
    repo.save_config(cfg)
    print(f"{key} = {value}")


# ---------- entries and commits ----------

def add(raw):
    repo = Repo()
    if not repo.ready:
        print("No work log for this repo; nothing logged.")
        return
    e = json.loads(raw)
    for k in ("date", "type", "title"):
        if not e.get(k):
            sys.exit(f"missing field: {k}")
    if e["type"] not in TYPES:
        sys.exit(f"type must be one of {', '.join(TYPES)}")
    e["id"] = hashlib.sha1((e["date"] + e["title"].lower()).encode()).hexdigest()[:10]
    if any(x.get("id") == e["id"] for x in read_jsonl(repo.entries)):
        print(f"exists {e['id']}")
        return
    e.setdefault("logged_at", dt.datetime.now().isoformat(timespec="seconds"))
    append_jsonl(repo.entries, e)
    print(f"added {e['id']}")


def list_entries(args):
    repo = Repo().need()
    today = dt.date.today()
    start, end = period(args) if args else ((today - dt.timedelta(days=14)).isoformat(), today.isoformat())
    hidden = set(repo.config.get("hidden", []))
    for e in sorted(read_jsonl(repo.entries), key=lambda x: x["date"]):
        if start <= e["date"] <= end:
            print(f"{e['id']}  {e['date']}  {e['type']:<13} {(e.get('project') or '-')[:24]:<24} {e.get('ticket') or 'no ticket':<10} {e['title']}{'  (hidden)' if e['id'] in hidden else ''}")


def list_projects():
    # One line per project name: cheap for Claude to read before every add, unlike `list all`.
    counts = collections.Counter(e.get("project") or "-" for e in read_jsonl(Repo().need().entries))
    for name, n in counts.most_common():
        print(f"{n:>3}  {name}")


def set_project(name, ids):
    repo = Repo().need()
    rows = read_jsonl(repo.entries)
    hit = 0
    for e in rows:
        if e["id"] in ids:
            e["project"] = name
            hit += 1
    with open(repo.entries, "w", encoding="utf-8") as f:
        f.writelines(json.dumps(e, ensure_ascii=False) + "\n" for e in rows)
    print(f"{hit} entries now under {name}")


def record_commit(repo, sha, cwd):
    if any(c["sha"] == sha[:11] for c in read_jsonl(repo.commits)):
        return
    fmt = git("show", "-s", "--format=%H%x1f%ad%x1f%an%x1f%s", "--date=short", sha, cwd=cwd).split("\x1f")
    if len(fmt) < 4:
        return
    branch = git("branch", "--contains", sha, "--format=%(refname:short)", cwd=cwd).splitlines()
    files = git("show", "--name-only", "--format=", sha, cwd=cwd).splitlines()
    ticket = next(iter(TICKET.findall(fmt[3] + " " + " ".join(branch))), "")
    append_jsonl(repo.commits, {"sha": fmt[0][:11], "date": fmt[1], "author": fmt[2], "subject": fmt[3],
                                "branches": branch[:5], "ticket": ticket, "files": files[:40], "repo": repo.name})


def commit_hook():
    # Called by Claude Code after Bash calls; acts only on `git commit` in repos that have a work log.
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    cmd = (payload.get("tool_input") or {}).get("command", "")
    if not re.search(r"\bgit\b.*\bcommit\b", cmd):
        return
    cwd = payload.get("cwd") or os.getcwd()
    repo = Repo(cwd)
    if not repo.ready:
        return
    email = repo.config.get("email")
    sha = git("rev-parse", "HEAD", cwd=cwd)
    if sha and (not email or git("show", "-s", "--format=%ae", sha, cwd=cwd) == email):
        record_commit(repo, sha, cwd)


def import_commits(since=None):
    repo = Repo().need()
    cfg = repo.config
    author = cfg.get("email") or cfg.get("name")
    if not author:
        sys.exit("No email or name in config. Run: worklog.py set email you@company.com")
    shas = git("log", "--all", "--no-merges", f"--author={author}", f"--since={since or cfg.get('since')} 00:00", "--format=%H", cwd=repo.root)
    seen = set()
    for sha in reversed(shas.splitlines()):
        subj = git("show", "-s", "--format=%ad %s", "--date=short", sha, cwd=repo.root)
        if subj in seen:  # same commit cherry-picked/rebased onto several branches
            continue
        seen.add(subj)
        record_commit(repo, sha, repo.root)
    print(f"{len(read_jsonl(repo.commits))} commits recorded")


# ---------- chat scanning ----------

def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(x.get("text", "") for x in content if isinstance(x, dict) and x.get("type") == "text")
    return ""


def clip(t, n):
    return t if len(t) <= n else t[:n] + "…"


BATCH_BYTES = 60_000  # ~15k tokens: one subagent reads one batch


def extract(all_chats=False):
    """Compact digests: each user message + Claude's final reply to it, trimmed, secrets masked, from the last scanned line.
    Claude's in-between narration is dropped (the final reply says what was done). Chats are packed into batch-N.txt files."""
    repo = Repo().need()
    state = {} if all_chats or not os.path.exists(repo.state) else json.load(open(repo.state))
    os.makedirs(repo.digests, exist_ok=True)
    for old in glob.glob(os.path.join(repo.digests, "*.txt")):
        os.remove(old)
    pending, chats = {}, []
    for path in sorted(glob.glob(repo.chat_glob())):
        start = state.get(path, 0)
        lines, reply, n = [], None, 0
        with open(path, encoding="utf-8", errors="ignore") as f:
            for n, line in enumerate(f, 1):
                if n <= start:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = o.get("type")
                if kind not in ("user", "assistant") or o.get("isMeta"):
                    continue
                t = text_of(o.get("message", {}).get("content")).strip()
                if not t or t.startswith(("<", "[Request interrupted")) or "This session is being continued" in t[:80]:
                    continue
                t = SECRET.sub("[redacted]", re.sub(r"\s+", " ", t))
                if kind == "assistant":
                    reply = t  # only the last reply before the next user message is kept
                    continue
                if reply:
                    lines.append(f"CLAUDE: {clip(reply, 400)}")
                    reply = None
                lines.append(f"{(o.get('timestamp') or '')[:10]} ME: {clip(t, 300)}")
        if reply:
            lines.append(f"CLAUDE: {clip(reply, 400)}")
        if lines:
            chats.append(f"# chat {os.path.basename(path)[:-6]}\n" + "\n".join(lines) + "\n\n")
        pending[path] = n
    json.dump(pending, open(os.path.join(repo.data, "scan-pending.json"), "w"))
    batches = [""]
    for c in chats:
        if batches[-1] and len(batches[-1]) + len(c) > BATCH_BYTES:
            batches.append("")
        batches[-1] += c
    for i, b in enumerate(filter(None, batches), 1):
        with open(os.path.join(repo.digests, f"batch-{i}.txt"), "w", encoding="utf-8") as f:
            f.write(b)
    files = sorted(glob.glob(os.path.join(repo.digests, "*.txt")))
    print(f"{len(chats)} chats with new messages, {sum(map(len, chats)) // 1024} KB in {len(files)} batch files:")
    print("\n".join(files))


def mark_scanned():
    repo = Repo().need()
    p = os.path.join(repo.data, "scan-pending.json")
    if not os.path.exists(p):
        sys.exit("nothing pending; run extract first")
    state = json.load(open(repo.state)) if os.path.exists(repo.state) else {}
    state.update(json.load(open(p)))
    json.dump(state, open(repo.state, "w"), indent=1)
    os.remove(p)
    print(f"{len(state)} chats marked as scanned")


# ---------- report ----------

PAGE = r"""<title>__TITLE__</title><meta name="claude:viewport-fit-sides" content="cover">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;600;700&family=IBM+Plex+Mono:wght@500;700&display=swap"><style>
/* Layout: ink header band with the totals, category tiles, projects, highlights, then the work grouped by project */
:root{--bg:#f4f5f8;--surface:#fff;--ink:#141a26;--mute:#5d6678;--line:#e1e5ec;--soft:#eef1f6;--band:#141a26;--band-ink:#f4f5f8;
 --feature:#2457c5;--fix:#0d7f57;--debugging:#c4450f;--investigation:#6d3bb5;--tooling:#9a6a12;--tk-bg:#e6edfb;--tk:#2457c5;--warn-bg:#fff2cf;--warn:#8a6100;
 --sans:"IBM Plex Sans","Segoe UI",system-ui,sans-serif;--mono:"IBM Plex Mono",ui-monospace,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0f131b;--surface:#171c27;--ink:#e8ebf2;--mute:#9aa3b5;--line:#283043;--soft:#1e2433;--band:#1d2433;--band-ink:#e8ebf2;
 --feature:#7ea3ff;--fix:#4cc59a;--debugging:#ff8a57;--investigation:#b893ff;--tooling:#e0b552;--tk-bg:#1f2b48;--tk:#9dbaff;--warn-bg:#3a3014;--warn:#f0c860;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#0f131b;--surface:#171c27;--ink:#e8ebf2;--mute:#9aa3b5;--line:#283043;--soft:#1e2433;--band:#1d2433;--band-ink:#e8ebf2;
 --feature:#7ea3ff;--fix:#4cc59a;--debugging:#ff8a57;--investigation:#b893ff;--tooling:#e0b552;--tk-bg:#1f2b48;--tk:#9dbaff;--warn-bg:#3a3014;--warn:#f0c860;color-scheme:dark}
[hidden]{display:none!important}
*{box-sizing:border-box;-webkit-print-color-adjust:exact;print-color-adjust:exact}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 var(--sans)}
main{max-width:920px;margin:0 auto;padding-block:24px 60px;padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px))}
a{color:inherit;text-decoration:none}
.hero{background:var(--band);color:var(--band-ink);border-radius:14px;padding:22px 24px}
.hero h1{margin:0;font-size:26px;letter-spacing:-.4px;text-wrap:balance}.hero p{margin:2px 0 0;opacity:.75}
.nums{display:flex;flex-wrap:wrap;gap:12px 28px;margin-top:16px}.nums b{display:block;font:700 28px/1 var(--mono);font-variant-numeric:tabular-nums}.nums span{font-size:12px;opacity:.75}
.cards{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin:12px 0}
.card{background:var(--surface);border:1px solid var(--line);border-top:4px solid var(--c);border-radius:10px;padding:10px;cursor:pointer;text-align:left;font:inherit;color:inherit}
.card b{display:block;font:700 24px/1.1 var(--mono);color:var(--c)}.card span{font-weight:700;font-size:13px}
.card:hover,.card:focus-visible{outline:2px solid var(--c);outline-offset:1px}.card.on{background:var(--c);color:var(--surface)}.card.on b{color:var(--surface)}
h2{font-size:12px;margin:22px 0 8px;text-transform:uppercase;letter-spacing:1px;color:var(--mute)}
.hl{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.h{background:var(--surface);border:1px solid var(--line);border-left:4px solid var(--c);border-radius:8px;padding:8px 11px;cursor:pointer;min-width:0}
.h b{display:block;font-size:13.5px}.h small{color:var(--mute);font-size:12px;display:block;margin-top:2px}
.proj{display:flex;flex-wrap:wrap;gap:6px}
.p{background:var(--surface);border:1px solid var(--line);border-radius:20px;padding:4px 12px;font-size:13px;cursor:pointer}.p b{color:var(--feature);font-family:var(--mono)}.p.on{background:var(--feature);color:var(--surface)}.p.on b{color:var(--surface)}
.tools{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:22px 0 8px}
.tools input{flex:1;min-width:0;padding:8px 12px;border:1px solid var(--line);border-radius:8px;font:inherit;background:var(--surface);color:var(--ink)}
.btn{border:1px solid var(--line);background:var(--surface);color:var(--ink);border-radius:8px;padding:8px 12px;font:inherit;font-weight:600;cursor:pointer}.btn.pri{background:var(--band);color:var(--band-ink);border-color:var(--band)}
.online .pdfbtn{display:none}
.count{color:var(--mute);font-size:13px;margin-bottom:6px}
details{background:var(--surface);border:1px solid var(--line);border-left:4px solid var(--c);border-radius:8px;margin:0 0 6px}
summary{display:flex;flex-wrap:wrap;gap:4px 8px;align-items:baseline;padding:8px 11px;cursor:pointer;list-style:none}summary::-webkit-details-marker{display:none}
summary .t{flex:1 1 260px;min-width:0;font-weight:600}.d{color:var(--mute);font-size:12px;white-space:nowrap;font-variant-numeric:tabular-nums}
.dot{width:8px;height:8px;border-radius:50%;background:var(--c);flex:none;align-self:center}
.tk{font:700 11px var(--mono);background:var(--tk-bg);color:var(--tk);border-radius:5px;padding:1px 6px;white-space:nowrap}
.body{padding:0 12px 10px 27px;font-size:13.5px}.body p{margin:3px 0}.body .r{background:var(--soft);border-radius:6px;padding:5px 8px}
.ev{display:flex;flex-wrap:wrap;gap:4px;margin-top:5px}.ev span{font-size:11.5px;color:var(--mute);background:var(--soft);border-radius:10px;padding:0 8px;overflow-wrap:anywhere}
.grp h3{display:flex;flex-wrap:wrap;gap:4px 8px;align-items:baseline;font-size:16px;margin:18px 0 6px;padding-bottom:4px;border-bottom:2px solid var(--line)}
.grp h3 .t{flex:1 1 240px}.grp h4{font-size:11px;text-transform:uppercase;letter-spacing:.8px;color:var(--mute);margin:8px 0 4px}.grp h4 b{font-family:var(--mono);color:var(--ink)}
.st{font-size:11px;font-weight:700;border-radius:4px;padding:0 5px;background:var(--warn-bg);color:var(--warn)}
@media (max-width:640px){.cards{grid-template-columns:repeat(2,1fr)}.hl{grid-template-columns:1fr}}
@media (prefers-reduced-motion:reduce){*{scroll-behavior:auto!important}}
@page{size:A4;margin:12mm}
@media print{:root{--bg:#fff;--surface:#fff;--ink:#141a26;--mute:#5d6678;--line:#e1e5ec;--soft:#eef1f6;--band:#141a26;--band-ink:#f4f5f8}
 body{font-size:11.5px}main{padding:0;max-width:none}.tools,.count{display:none}
 .card,.h,.p{break-inside:avoid}.hero{padding:14px 16px}.nums{margin-top:10px}.nums b{font-size:22px}h2{margin:12px 0 6px}
 .h{padding:5px 9px}.h b{font-size:11.5px}.h small{font-size:10px;margin:0}.h small:nth-of-type(1){display:-webkit-box;-webkit-line-clamp:1;-webkit-box-orient:vertical;overflow:hidden}.p{font-size:11px;padding:2px 9px}
 body.short #list,body.short #listh{display:none}#listh{break-before:page}
 .grp h3,.grp h4{break-after:avoid}.grp h3{font-size:13px;margin:10px 0 4px}
 details{border-radius:0;border-width:0 0 1px 3px;margin:0}summary{padding:3px 8px}details:not([open]) .body{display:none}}
</style><main class="__MODE__">
<div class="hero"><h1>What I worked on</h1><p>__WHO__ · __REPO__ · __SPAN__</p><div class="nums" id="nums"></div></div>
<div class="cards" id="cards"></div>
<h2 id="projh">Main projects</h2><div class="proj" id="proj"></div>
<h2>Highlights</h2><div class="hl" id="hl"></div>
<div class="tools"><input id="q" placeholder="Search work, tickets, tech…"><button class="btn" id="exp">Expand all</button>
<button class="btn pdfbtn" id="pdf1">PDF: 1 page</button><button class="btn pri pdfbtn" id="pdf2">PDF: with list</button></div>
<h2 id="listh">All work, by project</h2><div class="count" id="count"></div><div id="list"></div>
</main><script>
const D=__DATA__,TYPES=__TYPES__,JIRA="__JIRA__";
const $=s=>document.querySelector(s),esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"})[c]);
const md=d=>new Date(d+"T00:00").toLocaleDateString("en",{month:"short",day:"numeric"});
const when=e=>md(e.date)+(e.end&&e.end!==e.date?" – "+md(e.end):"");
const tks=s=>(s||"").match(/[A-Z]+-\d+/g)||[];
const chips=s=>tks(s).map(t=>JIRA?`<a class="tk" href="${JIRA}${t}" target="_blank" onclick="event.stopPropagation()">${t}</a>`:`<span class="tk">${t}</span>`).join(" ");
const first=(s,n)=>String(s||"").trim().split(/(?<=[.!?])\s+/).slice(0,n).join(" ");
const BUCKETS=[["Features",["feature"]],["Bug fixes",["fix"]],["Miscellaneous",["debugging","investigation","tooling"]]];
const end=e=>e.end||e.date;
let type=null,proj=null;
const allT=[...new Set(D.entries.flatMap(e=>tks(e.ticket)))];
$("#nums").innerHTML=[[D.entries.length,"pieces of work"],...(D.commits?[[D.commits,"commits"]]:[]),[allT.length,"tickets"],[D.entries.filter(e=>!e.ticket).length,"done outside tickets"],[D.entries.filter(e=>e.type==="debugging").length,"issues debugged"]].map(([n,l])=>`<div><b>${n}</b><span>${l}</span></div>`).join("");
$("#cards").innerHTML=Object.entries(TYPES).map(([k,l])=>`<button class="card" data-t="${k}" style="--c:var(--${k})"><b>${D.entries.filter(e=>e.type===k).length}</b><span>${l}</span></button>`).join("");
const counts={};D.entries.forEach(e=>{if(e.project!==D.other)counts[e.project]=(counts[e.project]||0)+1});
const projs=Object.entries(counts).filter(x=>x[1]>1).sort((a,b)=>b[1]-a[1]);
if(!projs.length){$("#projh").hidden=true;$("#proj").hidden=true}
const byId=Object.fromEntries(D.entries.map(e=>[e.id,e]));
$("#hl").innerHTML=D.highlights.map(i=>byId[i]).filter(Boolean).map(e=>`<div class="h" data-id="${e.id}" style="--c:var(--${e.type})"><b>${esc(e.title)}</b><small>${esc(first(e.outcome,1))}</small><small>${chips(e.ticket)} ${when(e)}</small></div>`).join("");
$("#proj").innerHTML=projs.map(([n,c])=>`<span class="p" data-k="${esc(n)}"><b>${c}</b> ${esc(n)}</span>`).join("");
const row=e=>`<details id="e-${e.id}" style="--c:var(--${e.type})"><summary><i class="dot"></i><span class="t">${esc(e.title)}</span>${e.status&&e.status!=="done"?`<span class="st ${e.status}">${e.status}</span>`:""}${chips(e.ticket)}<span class="d">${when(e)}</span></summary>
 <div class="body">${e.outcome?`<p class="r"><b>Result:</b> ${esc(first(e.outcome,2))}</p>`:""}${e.what?`<p><b>Did:</b> ${esc(first(e.what,2))}</p>`:""}${e.why?`<p><b>Why:</b> ${esc(first(e.why,1))}</p>`:""}
 ${(e.evidence||[]).length?`<div class="ev">${e.evidence.slice(0,5).map(x=>`<span>${esc(x)}</span>`).join("")}</div>`:""}</div></details>`;
function render(){const q=$("#q").value.toLowerCase();
 const rows=D.entries.filter(e=>(!type||e.type===type)&&(!proj||e.project===proj)&&(!q||JSON.stringify(e).toLowerCase().includes(q)));
 $("#count").textContent=`${rows.length} of ${D.entries.length} shown · click a row for details`;
 const groups={};rows.forEach(e=>(groups[e.project]??=[]).push(e));
 const last=g=>g.map(end).sort().pop();
 const order=Object.keys(groups).sort((a,b)=>(a===D.other)-(b===D.other)||last(groups[b]).localeCompare(last(groups[a])));
 $("#list").innerHTML=order.map(p=>{const g=groups[p];
  return `<section class="grp"><h3><span class="t">${esc(p)}</span>${chips([...new Set(g.flatMap(e=>tks(e.ticket)))].join(" "))}<span class="d">${g.length} item${g.length>1?"s":""} · ${md(g[0].date)} – ${md(last(g))}</span></h3>`
   +BUCKETS.map(([l,ts])=>{const b=g.filter(e=>ts.includes(e.type)).reverse();return b.length?`<h4>${l} <b>${b.length}</b></h4>${b.map(row).join("")}`:""}).join("")+`</section>`}).join("");
 document.querySelectorAll(".card").forEach(c=>c.classList.toggle("on",c.dataset.t===type));
 document.querySelectorAll(".p").forEach(c=>c.classList.toggle("on",c.dataset.k===proj));}
$("#cards").onclick=ev=>{const c=ev.target.closest(".card");if(c){type=type===c.dataset.t?null:c.dataset.t;render();$("#listh").scrollIntoView({behavior:"smooth"})}};
$("#proj").onclick=ev=>{const c=ev.target.closest(".p");if(c){proj=proj===c.dataset.k?null:c.dataset.k;render();$("#listh").scrollIntoView({behavior:"smooth"})}};
$("#hl").onclick=ev=>{const h=ev.target.closest(".h");if(!h)return;type=proj=null;$("#q").value="";render();const d=$("#e-"+h.dataset.id);d.open=true;d.scrollIntoView({behavior:"smooth",block:"center"})};
$("#q").oninput=render;
$("#exp").onclick=()=>{const all=[...document.querySelectorAll("details")],o=!all.every(d=>d.open);all.forEach(d=>d.open=o);$("#exp").textContent=o?"Collapse all":"Expand all"};
const print=s=>{document.body.classList.toggle("short",s);window.print()};
$("#pdf1").onclick=()=>print(true);$("#pdf2").onclick=()=>print(false);
render();if(location.hash==="#short")document.body.classList.add("short");
</script>"""


def find_chrome():
    for c in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome", "msedge"):
        if shutil.which(c):
            return shutil.which(c)
    for p in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/Applications/Chromium.app/Contents/MacOS/Chromium",
              "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"):
        if os.path.exists(p):
            return p
    return None


def auto_highlights(entries):
    """Without hand-picked highlights: newest features first, then other ticketed work, up to 10."""
    rank = {"feature": 0, "debugging": 1, "fix": 2, "investigation": 3, "tooling": 4}
    newest = sorted(entries, key=lambda e: e["date"], reverse=True)
    return [e["id"] for e in sorted(newest, key=lambda e: (rank[e["type"]], not e.get("ticket")))[:10]]


def project_of(e, names):
    """Entry's own project, else the name given to its ticket in config, else the ticket id, else Other work."""
    tickets = TICKET.findall(e.get("ticket") or "")
    return e.get("project") or next((names[t] for t in tickets if t in names), None) or (tickets[0] if tickets else OTHER)


def pdf(args):
    repo = Repo().need()
    cfg = repo.config
    entries = sorted(read_jsonl(repo.entries), key=lambda x: x["date"])
    start, end = period(args, "all", entries[0]["date"] if entries else None)
    hidden = set(cfg.get("hidden", []))
    entries = [e for e in entries if start <= e["date"] <= end and e["id"] not in hidden]
    commits = list({c["sha"][:9]: c for c in read_jsonl(repo.commits)
                    if start <= c["date"] <= end and not NOISE.search(c["subject"])}.values())
    keep = ("id", "type", "date", "end", "title", "ticket", "what", "why", "outcome", "evidence", "status", "env", "handed_to")
    names = cfg.get("projects") or {}
    data = {"entries": [{**{k: e.get(k) for k in keep}, "project": project_of(e, names)} for e in entries], "commits": len(commits),
            "highlights": cfg.get("highlights") or auto_highlights(entries), "other": OTHER}
    fmt_date = lambda d: (lambda x: f"{x:%b} {x.day}, {x.year}")(dt.date.fromisoformat(d))
    who = cfg.get("name") or "My"
    labels = {"feature": "Features", "fix": "Fixes", "debugging": "Debugging", "investigation": "Investigations", "tooling": "Tooling & help"}
    doc = (PAGE.replace("__DATA__", json.dumps(data).replace("</", "<\\/")).replace("__TYPES__", json.dumps(labels))
           .replace("__JIRA__", cfg.get("jira", "")).replace("__TITLE__", html.escape(f"{who.split()[0]}'s Work Log" if cfg.get("name") else "Work Log"))
           .replace("__WHO__", html.escape(cfg.get("name", ""))).replace("__REPO__", html.escape(repo.name))
           .replace("__SPAN__", f"{fmt_date(start)} – {fmt_date(end)}"))
    out_dir = os.path.join(HOME, "Downloads") if os.path.isdir(os.path.join(HOME, "Downloads")) else HOME
    html_path = os.path.join(out_dir, f"worklog-{repo.name}.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">\n' + doc.replace("__MODE__", "local"))
    online = os.path.join(repo.data, "worklog-online.html")
    with open(online, "w", encoding="utf-8") as f:
        f.write(doc.replace("__MODE__", "online"))
    full = "--full" in args
    out = os.path.join(out_dir, f"worklog-{repo.name}-{start}-to-{end}{'' if full else '-summary'}.pdf")
    print(f"html: {html_path}")
    print(f"online page file: {online}")
    chrome = find_chrome()
    if not chrome:
        print(f"pdf: no Chrome/Chromium/Edge found. Open {html_path} and use its PDF buttons.")
        return
    if os.path.exists(out):
        os.remove(out)
    subprocess.run([chrome, "--headless", "--disable-gpu", "--no-pdf-header-footer", f"--print-to-pdf={out}",
                    "file://" + html_path.replace(os.sep, "/") + ("" if full else "#short")], capture_output=True, timeout=120)
    print(f"pdf: {out}" if os.path.exists(out) else f"pdf: failed. Open {html_path} and use its PDF buttons.")


if __name__ == "__main__":
    a = sys.argv[1:]
    cmd = a[0] if a else "list"
    rest = a[1:]
    opt = lambda name, default=None: rest[rest.index(name) + 1] if name in rest and rest.index(name) + 1 < len(rest) else default
    if cmd == "init":
        init()
    elif cmd == "install":
        install()
    elif cmd == "status":
        status()
    elif cmd == "add" and rest:
        add(rest[0])
    elif cmd == "projects":
        list_projects()
    elif cmd == "project" and len(rest) >= 2:
        set_project(rest[0], set(rest[1:]))
    elif cmd == "list":
        list_entries(rest)
    elif cmd == "session-context":
        session_context()
    elif cmd == "commit-hook":
        commit_hook()
    elif cmd == "commits":
        import_commits(opt("--since"))
    elif cmd == "extract":
        extract("--all" in rest)
    elif cmd == "mark-scanned":
        mark_scanned()
    elif cmd == "pdf":
        pdf(rest)
    elif cmd == "set" and len(rest) == 2:
        set_key(*rest)
    elif cmd == "get" and rest:
        print(Repo().need().config.get(rest[0], ""))
    else:
        print(__doc__)
