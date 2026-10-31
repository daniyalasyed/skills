#!/usr/bin/env python3
"""Helper for the moving-claude-project-folders skill.

Run `python3 claude_move.py <command> --help` for each command's options.
On Windows use `python` or `py -3` in place of `python3`.

Commands that change nothing (apart from writing their own report files):
  inventory     everything tied to a folder, and every file that writes its path
  list-chats    the chats in one chat folder: ID, dates, title, working folder
  check-plan    check a plan against the disk and write the change list
  check-closed  running chats and editor windows inside the given folders
  verify        check the result of an applied plan
  prefix-check  before deleting a backup: did every backed-up chat only grow?

Commands that change things (run only after the user approved the plan):
  backup        copy what the plan changes, with SHA-256 fingerprints, and verify the copy
  test-chat     make a throwaway chat headless, move it, check it, remove it
  apply         carry out the plan, one action at a time, stopping at the first problem
  revert        undo an applied plan from its backup

Needs Python 3.8 or later and nothing else. Never edits a chat file (.jsonl),
never copies a chat inside Claude Code's projects folder, and runs
`claude purge` only with --dry-run.
"""
import argparse
import csv
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
FOLD_CASE = IS_WIN or IS_MAC  # default file systems on Windows and macOS ignore letter case
MAX_NAME = 200  # longer chat-folder names are cut and get a hash Claude Code doesn't document
MAX_TEXT_BYTES = 5_000_000
TEXT_EXT = {
    ".md", ".markdown", ".txt", ".json", ".jsonc", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".sh", ".bash", ".zsh", ".fish", ".ps1", ".psm1",
    ".bat", ".cmd", ".html", ".htm", ".css", ".csv", ".xml", ".desktop", ".code-workspace",
}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox", ".next", ".cache"}
SECRET_RE = re.compile(
    r"(^\.env(\..*)?$)|(\.(key|pem|p12|pfx|keystore|jks)$)|(^\.?credentials(\.json)?$)|(^secrets?(\..*)?$)", re.I)
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
BOUNDARY = r"(?![A-Za-z0-9_\-])"
TEST_PROMPT = "Throwaway test chat for moving-claude-project-folders. Reply with OK only."
TEST_PROMPT_2 = "Second message, sent after the move. Reply with OK only."
# Test chats run with hooks off: a plugin's session hook can write into the folder and keep it busy
# after the chat ends (seen on Windows: the folder couldn't be renamed for a few minutes).
NO_HOOKS = json.dumps({"disableAllHooks": True})

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


class Stop(Exception):
    """A condition under the skill's stop rule."""


def stop(msg):
    raise Stop(msg)


# ---------------------------------------------------------------- paths and names

def norm(p):
    return os.path.normpath(os.path.abspath(os.path.expanduser(str(p))))


def fold(s):
    return s.lower() if FOLD_CASE else s


def same_path(a, b):
    return fold(norm(a)) == fold(norm(b))


def is_under(child, parent):
    c, p = fold(norm(child)), fold(norm(parent))
    return c == p or c.startswith(p.rstrip("\\/") + os.sep)


def config_dir():
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return norm(env) if env else norm(os.path.join("~", ".claude"))


def projects_dir():
    return os.path.join(config_dir(), "projects")


def claude_json_path():
    cands = []
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        cands.append(os.path.join(norm(env), ".claude.json"))
    cands.append(norm(os.path.join("~", ".claude.json")))
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


def chat_folder_name(path):
    """Claude Code's rule: the full path with every character that isn't a letter or digit turned into '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", norm(path))


def list_chat_folders():
    pd = projects_dir()
    if not os.path.isdir(pd):
        return []
    return sorted(n for n in os.listdir(pd) if os.path.isdir(os.path.join(pd, n)))


def find_chat_folder(name, names=None):
    names = list_chat_folders() if names is None else names
    return [n for n in names if fold(n) == fold(name)]


def short(p):
    home = norm("~")
    p = str(p)
    return "~" + p[len(home):] if is_under(p, home) else p


def stamp():
    return datetime.datetime.now().strftime("%y%m%d-%H%M%S")


def default_work_dir():
    return norm(os.path.join("~", "claude-move-backups", stamp()))


# ---------------------------------------------------------------- files

def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_text(path):
    try:
        if os.path.getsize(path) > MAX_TEXT_BYTES:
            return None
        with open(path, "rb") as f:
            return f.read().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def split_lines(text):
    parts = text.split("\n")
    lines = [p + "\n" for p in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def body(line):
    return line.rstrip("\r\n")


def ending(line):
    return line[len(body(line)):]


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def is_secret(name):
    return bool(SECRET_RE.search(name))


def in_skip(path, skip):
    return any(is_under(path, s) for s in skip)


def walk_files(root, skip, exts=TEXT_EXT):
    """Text files under root, never entering skipped (private) folders."""
    if os.path.isfile(root):
        if os.path.splitext(root)[1].lower() in exts and not is_secret(os.path.basename(root)):
            yield root
        return
    for dp, dn, fn in os.walk(root):
        if in_skip(dp, skip):
            dn[:] = []
            continue
        dn[:] = [d for d in dn if d not in SKIP_DIRS and not in_skip(os.path.join(dp, d), skip)]
        for f in fn:
            if os.path.splitext(f)[1].lower() in exts and not is_secret(f):
                yield os.path.join(dp, f)


def config_search_files(skip):
    """Claude Code's own text files that can name a folder. Chat transcripts are excluded on purpose."""
    cd = config_dir()
    out = []
    for n in ("CLAUDE.md", "settings.json", "settings.local.json", "keybindings.json"):
        p = os.path.join(cd, n)
        if os.path.isfile(p):
            out.append(p)
    for sub in ("skills", "rules", "commands", "agents", "output-styles", "hooks", "workflows", "agent-memory"):
        d = os.path.join(cd, sub)
        if os.path.isdir(d):
            out.extend(walk_files(d, skip))
    pl = os.path.join(cd, "plugins")
    if os.path.isdir(pl):
        out.extend(os.path.join(pl, f) for f in os.listdir(pl) if f.endswith(".json"))
    for n in list_chat_folders():
        m = os.path.join(projects_dir(), n, "memory")
        if os.path.isdir(m):
            out.extend(walk_files(m, skip))
    return out


# ---------------------------------------------------------------- chats

def chat_ids(folder):
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    return sorted(f[:-6] for f in names if f.endswith(".jsonl") and UUID_RE.match(f[:-6])
                  and os.path.isfile(os.path.join(folder, f)))


def chat_parts(folder, cid):
    """Everything that belongs to one chat: <id>.jsonl, the <id> folder and set-aside copies."""
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    return sorted(n for n in names if n == cid + ".jsonl" or n == cid
                  or n.startswith(cid + ".orphaned-") or n.startswith(cid + ".jsonl.superseded-"))


def folder_stats(folder):
    ids = chat_ids(folder)
    names = os.listdir(folder) if os.path.isdir(folder) else []
    idset = set(ids)
    dirs = [n for n in names if os.path.isdir(os.path.join(folder, n)) and n != "memory"]
    mem = os.path.join(folder, "memory")
    notes = [f for f in os.listdir(mem) if f.endswith(".md") and f != "MEMORY.md"] if os.path.isdir(mem) else []
    size = 0
    for dp, _, fs in os.walk(folder):
        for f in fs:
            try:
                size += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    return {
        "chats": len(ids),
        "chat_folders": len([d for d in dirs if d in idset]),
        "folders_without_chat": len([d for d in dirs if d not in idset]),
        "set_aside_copies": len([n for n in names if ".orphaned-" in n or ".superseded-" in n]),
        "memory_notes": len(notes),
        "memory_index": os.path.isfile(os.path.join(mem, "MEMORY.md")),
        "bytes": size,
    }


def first_cwd(path, max_lines=400):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                if '"cwd"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if isinstance(o, dict) and o.get("cwd"):
                    return o["cwd"]
    except OSError:
        return None
    return None


def cwds_after(path, start_line):
    """Working folders recorded on the lines after start_line (0-based count of lines to skip)."""
    out = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            if i < start_line or '"cwd"' not in line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if isinstance(o, dict) and o.get("cwd"):
                out[o["cwd"]] = out.get(o["cwd"], 0) + 1
    return out


def chat_title(path, max_lines=600):
    title = first = None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                if '"customTitle"' not in line and '"user"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(o, dict):
                    continue
                if o.get("customTitle"):
                    title = o["customTitle"]
                if first is None and o.get("type") == "user":
                    c = (o.get("message") or {}).get("content")
                    if isinstance(c, str):
                        first = c
                    elif isinstance(c, list):
                        for part in c:
                            if isinstance(part, dict) and part.get("type") == "text":
                                first = part.get("text")
                                break
    except OSError:
        return "(unreadable)"
    return re.sub(r"\s+", " ", title or first or "(no title)").strip()[:70]


def chat_started(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i > 50:
                    break
                m = re.search(r'"timestamp":"([0-9T:\-.]+)', line)
                if m:
                    return m.group(1)[:10]
    except OSError:
        pass
    return datetime.datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d")


def nlines(path):
    with open(path, "rb") as f:
        return sum(1 for _ in f)


def first_line_sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.readline()).hexdigest()


# ---------------------------------------------------------------- running chats and windows

def pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if IS_WIN:
        # os.kill(pid, 0) is not a check on Windows: signal 0 there is Ctrl+C.
        r = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/NH", "/FO", "CSV"],
                           capture_output=True, text=True)
        return ('"%d"' % pid) in r.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def live_records():
    """(kind, pid, folders) for running chats and editor windows. Lock files hold an access
    token, so only the process ID and folders are ever read out of them."""
    out = []
    cd = config_dir()
    for kind, sub, ext, key in (("chat", "sessions", ".json", "cwd"), ("window", "ide", ".lock", "workspaceFolders")):
        d = os.path.join(cd, sub)
        if not os.path.isdir(d):
            continue
        for n in sorted(os.listdir(d)):
            if not n.endswith(ext):
                continue
            try:
                o = load_json(os.path.join(d, n))
            except (OSError, ValueError):
                continue
            if not isinstance(o, dict):
                continue
            folders = o.get(key)
            folders = [folders] if isinstance(folders, str) else folders
            folders = [x for x in (folders or []) if isinstance(x, str)]
            if folders and pid_alive(o.get("pid")):
                out.append((kind, o.get("pid"), folders, o.get("sessionId")))
    return out


def open_inside(paths):
    hits = []
    for kind, pid, folders, _sid in live_records():
        for fo in folders:
            if any(is_under(fo, p) for p in paths):
                hits.append((kind, pid, fo))
    return hits


def open_chats(ids):
    """Running chats among these chat IDs. A chat move needs only the moving chats closed, not their folders."""
    ids = set(ids)
    return [("chat " + sid, pid, folders[0]) for kind, pid, folders, sid in live_records()
            if kind == "chat" and sid in ids]


def describe(hits):
    return "; ".join("%s (process %s) in %s" % h for h in hits)


# ---------------------------------------------------------------- Claude Code CLI

def claude_exe():
    return shutil.which("claude")


def run_cli(args, cwd=None, timeout=180):
    exe = claude_exe()
    if not exe:
        return None
    return subprocess.run([exe] + args, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


_PURGE = None


def purge_cmd():
    """`claude purge` on v2.1.288 and later, `claude project purge` before. Chosen from --help output."""
    global _PURGE
    if _PURGE is None:
        _PURGE = []
        for sub in (["purge"], ["project", "purge"]):
            r = run_cli(sub + ["--help"], timeout=60)
            if r is not None and r.returncode == 0 and "--dry-run" in r.stdout:
                _PURGE = sub
                break
    return _PURGE


def purge_dry_run(path):
    sub = purge_cmd()
    if not sub:
        return None
    args = sub + [norm(path), "--dry-run"]
    if "--dry-run" not in args:  # without --dry-run, purge deletes
        stop("refusing to run purge without --dry-run")
    return run_cli(args, timeout=180)


# ---------------------------------------------------------------- git and memory

def nearest_existing(path):
    cur = norm(path)
    while not os.path.exists(cur):
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent
    return cur


def git_root(path):
    """The git repository root above path, as Claude Code keys auto memory: a worktree maps to
    its main checkout. None outside a repository."""
    cur = nearest_existing(path)
    while cur:
        g = os.path.join(cur, ".git")
        if os.path.isdir(g):
            return cur
        if os.path.isfile(g):
            try:
                with open(g, encoding="utf-8") as f:
                    m = re.match(r"gitdir:\s*(.+)", f.read().strip())
            except OSError:
                m = None
            if m:
                gd = norm(os.path.join(cur, m.group(1).strip()))
                parts = re.split(r"[\\/]\.git[\\/]worktrees[\\/]", gd)
                if len(parts) == 2:
                    return norm(parts[0])
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent
    return None


def memory_owner(path):
    return git_root(path) or norm(path)


def auto_memory_setting(folder):
    found = []
    for p in (os.path.join(config_dir(), "settings.json"), os.path.join(folder, ".claude", "settings.json"),
              os.path.join(folder, ".claude", "settings.local.json")):
        try:
            if os.path.isfile(p) and "autoMemoryDirectory" in load_json(p):
                found.append(p)
        except (OSError, ValueError):
            pass
    return found


def run_git(args):
    if not shutil.which("git"):
        return None
    return subprocess.run(["git"] + args, capture_output=True, text=True, encoding="utf-8", errors="replace")


# ---------------------------------------------------------------- path spellings

def _rel_home(p):
    home = norm("~")
    if is_under(p, home) and not same_path(p, home):
        return p[len(home):].lstrip("\\/")
    return None


def _forms(p):
    if IS_WIN:
        forms = {"native": p, "fwd": p.replace("\\", "/"), "dbl": p.replace("\\", "\\\\")}
        m = re.match(r"^([A-Za-z]):[\\/](.*)$", p)
        if m:
            rest = m.group(2).replace("\\", "/")
            forms["gitbash"] = "/" + m.group(1).lower() + "/" + rest
            forms["wsl"] = "/mnt/" + m.group(1).lower() + "/" + rest
        r = _rel_home(p)
        if r is not None:
            forms["tilde_fwd"] = "~/" + r.replace("\\", "/")
            forms["tilde_back"] = "~\\" + r
            forms["userprofile"] = "%USERPROFILE%\\" + r
        return forms
    forms = {"native": p}
    r = _rel_home(p)
    if r is not None:
        forms["tilde_fwd"] = "~/" + r
        forms["home_var"] = "$HOME/" + r
    return forms


_FALLBACK = {"tilde_fwd": "fwd", "tilde_back": "native", "userprofile": "native", "home_var": "native",
             "gitbash": "fwd", "wsl": "fwd", "dbl": "dbl", "fwd": "fwd"}


def spelling_pairs(old, new):
    """(old spelling, new spelling) for each way a path is commonly written, longest first."""
    fo, fn = _forms(norm(old)), _forms(norm(new))
    pairs = {}
    for k, o in fo.items():
        n = fn.get(k) or fn.get(_FALLBACK.get(k, "native")) or fn["native"]
        pairs.setdefault(fold(o), (o, n))
    return sorted(pairs.values(), key=lambda x: -len(x[0]))


def build_regex(olds):
    olds = sorted(set(olds), key=len, reverse=True)
    if not olds:
        return None
    return re.compile("(?:" + "|".join(re.escape(o) for o in olds) + ")" + BOUNDARY, re.I if FOLD_CASE else 0)


def to_native(s):
    s = s.replace("\\\\", "\\")
    if IS_WIN:
        m = re.match(r"^/mnt/([a-zA-Z])/(.*)$", s) or re.match(r"^/([a-zA-Z])/(.*)$", s)
        if m:
            s = m.group(1).upper() + ":\\" + m.group(2)
        s = re.sub(r"(?i)^%USERPROFILE%", lambda _m: norm("~"), s)
        s = s.replace("/", "\\")
    if s.startswith("$HOME"):
        s = norm("~") + s[5:]
    if s.startswith("~"):
        s = norm("~") + s[1:]
    return s


def all_pairs(plan):
    """Every (old spelling, new spelling) a plan implies: folder paths and chat-folder names."""
    pairs = []
    for m in plan.get("folder_moves", []) or plan.get("path_changes", []):
        pairs.extend(spelling_pairs(m["from"], m["to"]))
    for r in plan.get("chat_folder_renames", []):
        pairs.append((r["from"], r["to"]))
    return pairs


def find_hits(files, regex, literal_regex=None):
    hits = []
    for p in files:
        text = read_text(p)
        if text is None:
            continue
        for i, line in enumerate(split_lines(text), 1):
            b = body(line)
            ms = [m.group(0) for m in regex.finditer(b)] if regex else []
            ls = [m.group(0) for m in literal_regex.finditer(b)] if literal_regex else []
            if ms or ls:
                hits.append({"file": p, "line": i, "text": b, "matches": ms, "literals": ls})
    return hits


def snippet(text, needle, width=60):
    i = fold(text).find(fold(needle)) if needle else 0
    i = max(i, 0)
    a, b = max(0, i - width), min(len(text), i + len(needle or "") + width)
    return ("…" if a else "") + text[a:b] + ("…" if b < len(text) else "")


def diff_window(before, after, width=70):
    i = 0
    while i < min(len(before), len(after)) and before[i] == after[i]:
        i += 1
    a = max(0, i - width)
    eb, ea = len(before), len(after)
    while eb > i and ea > i and before[eb - 1] == after[ea - 1]:
        eb -= 1
        ea -= 1
    pre = "…" if a else ""
    return (pre + before[a:min(len(before), eb + width)] + ("…" if eb + width < len(before) else ""),
            pre + after[a:min(len(after), ea + width)] + ("…" if ea + width < len(after) else ""))


# ---------------------------------------------------------------- plan helpers

def load_plan(path):
    plan = load_json(path)
    for k in ("folder_moves", "chat_folder_renames", "chat_moves", "memory_moves", "line_changes",
              "leave_as_is", "search_roots", "skip", "also_search", "closed_check", "backup_copy",
              "backup_fingerprint"):
        plan.setdefault(k, [])
    return plan


def map_path(p, plan):
    """Where a file named by its original path is after the plan's moves."""
    p = norm(p)
    pd = projects_dir()
    for r in plan.get("chat_folder_renames", []):
        src = os.path.join(pd, r["from"])
        if is_under(p, src):
            p = os.path.join(pd, r["to"]) + p[len(src):]
            break
    for m in plan.get("chat_moves", []):
        src = os.path.join(pd, m["from_chat_folder"])
        if is_under(p, src) and not same_path(p, src):
            rel = p[len(src):].lstrip("\\/")
            first = re.split(r"[\\/]", rel)[0]
            if any(first == c + ".jsonl" or first == c or first.startswith(c + ".") for c in m["ids"]):
                p = os.path.join(pd, m["to_chat_folder"], rel)
    for mm in plan.get("memory_moves", []):
        for note in mm["notes"]:
            if same_path(p, os.path.join(mm["from_memory"], note)):
                p = os.path.join(norm(mm["to_memory"]), note)
    for fm in plan.get("folder_moves", []):
        src = norm(fm["from"])
        if is_under(p, src):
            p = norm(fm["to"]) + p[len(src):]
            break
    return p


def index_lines_for(index_text, note):
    pat = re.compile(r"\]\(\s*<?(\./)?" + re.escape(note) + r">?\s*\)")
    return [i for i, line in enumerate(split_lines(index_text)) if pat.search(line)]


def manifest_rows(backup_dir):
    p = os.path.join(backup_dir, "manifest.csv")
    if not os.path.isfile(p):
        stop("no backup manifest at " + p + " (run backup first)")
    with open(p, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def backup_of(rows):
    return {fold(norm(r["original_path"])): r for r in rows}


# ================================================================ inventory

def cmd_inventory(a):
    old = norm(a.old)
    new = norm(a.new) if a.new else None
    skip = [norm(s) for s in (a.skip or [])]
    wd = norm(a.work_dir) if a.work_dir else default_work_dir()
    stops, checks, notes = [], [], []
    names = list_chat_folders()
    say = print

    say("== Environment")
    r = run_cli(["--version"], timeout=60)
    say("  Claude Code: " + (r.stdout.strip() if r is not None and r.returncode == 0 else "not found on PATH"))
    say("  system: %s; Python %s" % (sys.platform, sys.version.split()[0]))
    say("  Claude Code data folder: " + config_dir())
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        notes.append("CLAUDE_CONFIG_DIR is set: Claude Code keeps its data in " + config_dir())
    if os.environ.get("CLAUDE_CODE_PROJECT_DIR_NAME"):
        stops.append("CLAUDE_CODE_PROJECT_DIR_NAME is set: chats aren't filed by folder path in this setup, so this skill doesn't apply")
    try:
        cpd = load_json(os.path.join(config_dir(), "settings.json")).get("cleanupPeriodDays")
    except (OSError, ValueError):
        cpd = None
    if cpd is None:
        notes.append("cleanupPeriodDays isn't set in user settings: Claude Code deletes chats older than 30 days (default). A move doesn't change this.")
    else:
        say("  cleanupPeriodDays: %s" % cpd)

    say("\n== The folder")
    old_exists, new_exists = os.path.isdir(old), bool(new and os.path.exists(new))
    recovery = (not old_exists) and new_exists
    say("  from: %s (%s)" % (old, "exists" if old_exists else "doesn't exist"))
    if new:
        say("  to:   %s (%s)" % (new, "exists" if new_exists else "doesn't exist yet"))
    if old_exists and os.path.islink(old):
        stops.append("the folder is a symbolic link; moving links isn't covered by this skill")
    if old_exists and IS_WIN:
        try:
            if os.lstat(old).st_file_attributes & 0x400:  # reparse point: junction or link
                stops.append("the folder is a junction or link; moving links isn't covered by this skill")
        except (OSError, AttributeError):
            pass
    if old_exists and new_exists:
        stops.append("both the old and the new folder exist; decide with the user which is the real one")
    if not old_exists and not new_exists:
        stops.append("neither the old nor the new folder exists")
    if recovery:
        notes.append("RECOVERY: the folder has already been moved; the draft plan moves the chat folders after it")
    if new and old_exists and not new_exists:
        if is_under(new, old):
            stops.append("the new path is inside the old folder")
        parent = os.path.dirname(new)
        if not os.path.isdir(parent):
            stops.append("the new folder's parent doesn't exist: " + parent)
        else:
            try:
                if os.stat(old).st_dev != os.stat(parent).st_dev:
                    stops.append("the new place is on another drive or file system; this skill moves by renaming on one drive only")
            except OSError:
                pass

    say("\n== Chat folders tied to the folder")
    base_root = old if old_exists else (new if recovery else old)
    sub = {}
    repos = []
    if os.path.isdir(base_root):
        for dp, dn, fn in os.walk(base_root):
            if in_skip(dp, skip):
                dn[:] = []
                continue
            if ".git" in dn or ".git" in fn:
                repos.append(dp)
            dn[:] = [d for d in dn if d not in SKIP_DIRS and not in_skip(os.path.join(dp, d), skip)]
            rel = dp[len(base_root):]
            sub.setdefault(fold(chat_folder_name(old + rel)), old + rel)
    base = fold(chat_folder_name(old))
    cands = [n for n in names if fold(n) == base or fold(n).startswith(base + "-")]
    parent = os.path.dirname(old)
    siblings = {}
    if os.path.isdir(parent):
        for s in os.listdir(parent):
            sp = os.path.join(parent, s)
            if os.path.isdir(sp) and not same_path(sp, old):
                siblings[fold(chat_folder_name(sp))] = sp
    renames = []
    report_names = []
    for n in cands:
        f = fold(n)
        inside = sub.get(f)
        elsewhere = None
        for k, sp in siblings.items():
            if f == k or f.startswith(k + "-"):
                elsewhere = sp
        folder = os.path.join(projects_dir(), n)
        for cid in chat_ids(folder)[:20]:
            c = first_cwd(os.path.join(folder, cid + ".jsonl"))
            if c and os.path.isdir(c) and not is_under(c, base_root) and fold(chat_folder_name(c)) == f:
                elsewhere = norm(c)
                break
        st = folder_stats(folder)
        desc = "%d chats, %d notes, %.1f MB" % (st["chats"], st["memory_notes"], st["bytes"] / 1e6)
        if inside and not elsewhere:
            status = "moves with it (folder %s)" % short(inside)
            if new:
                rel = inside[len(old):]
                to = chat_folder_name(new + rel)
                renames.append({"from": n, "to": to})
        elif inside and elsewhere:
            status = "AMBIGUOUS: matches %s and %s" % (short(inside), short(elsewhere))
            checks.append("chat folder %s could belong to two folders; ask the user" % n)
        elif elsewhere:
            status = "belongs to another folder: " + short(elsewhere)
        else:
            status = "UNCONFIRMED: no folder on disk matches this name"
            checks.append("chat folder %s matches by name only (a deleted or private subfolder, or a name with '-' or '_'); ask the user" % n)
        if inside or not elsewhere:
            report_names.append(n)
        say("  %s\n      %s; %s" % (n, desc, status))
    if not cands:
        say("  none (Claude Code has no chats or memory for this folder)")
    if new:
        for r_ in renames:
            if len(r_["to"]) > MAX_NAME:
                stops.append("new chat-folder name over %d characters (%s…): Claude Code adds a hash this skill can't compute" % (MAX_NAME, r_["to"][:40]))
            clash = find_chat_folder(r_["to"], names)
            if clash and fold(r_["to"]) != fold(r_["from"]):
                st = folder_stats(os.path.join(projects_dir(), clash[0]))
                if recovery:
                    notes.append("chat folder %s already exists (%d chats, %d notes): the draft plan merges into it with chat and memory moves" % (clash[0], st["chats"], st["memory_notes"]))
                else:
                    stops.append("chat folder %s already exists (%d chats, %d notes): another folder maps to the same name, or the new path was opened early" % (clash[0], st["chats"], st["memory_notes"]))
            say("  rename: %s -> %s" % (r_["from"], r_["to"]))

    say("\n== Memory")
    owner = memory_owner(base_root)
    if not same_path(owner, base_root):
        say("  this folder is inside the git repository %s; its chats read that repository's memory" % short(owner))
        if new and git_root(os.path.dirname(new)) and not same_path(git_root(os.path.dirname(new)), owner):
            checks.append("after the move the folder is inside a different git repository: its chats will read %s's memory" % short(git_root(os.path.dirname(new))))
        elif new and not git_root(os.path.dirname(new)):
            checks.append("after the move the folder is outside any git repository: its chats get their own, empty memory; move notes with a memory move if needed")
    else:
        say("  memory folder: " + os.path.join(projects_dir(), chat_folder_name(base_root), "memory"))
        if new and not os.path.exists(os.path.join(base_root, ".git")):
            nr = git_root(os.path.dirname(new))
            if nr:
                checks.append("after the move the folder is inside the git repository %s: its chats will read that repository's memory, not this folder's own" % short(nr))
    for p in auto_memory_setting(base_root):
        notes.append("autoMemoryDirectory is set in %s: memory lives where that setting points and doesn't move" % short(p))
    for rp in repos:
        g = run_git(["-C", rp, "worktree", "list", "--porcelain"])
        if g is not None and g.returncode == 0:
            n = len([l for l in g.stdout.splitlines() if l.startswith("worktree ")])
            if n > 1:
                checks.append("git repository %s has %d worktrees; after the move run `git worktree repair` there, with the user's yes" % (short(rp), n))
    g = run_git(["config", "--global", "--get-all", "safe.directory"])
    if g is not None and g.returncode == 0:
        for l in g.stdout.splitlines():
            if l.strip() and l.strip() != "*" and is_under(to_native(l.strip()), old):
                checks.append("global git safe.directory names the old path: " + l.strip())
    say("  git repositories inside: %d" % len(repos))

    say("\n== What Claude Code itself lists (claude purge --dry-run)")
    r = purge_dry_run(old)
    if r is None:
        say("  skipped: the claude command isn't available")
    else:
        say("  exit code %d" % r.returncode)
        for l in (r.stdout + r.stderr).strip().splitlines():
            say("  | " + l)

    say("\n== Claude Code's settings file (.claude.json): left alone, reported only")
    cj = claude_json_path()
    entries = []
    if cj:
        try:
            projs = load_json(cj).get("projects") or {}
        except (OSError, ValueError):
            projs = {}
        for k, v in projs.items():
            if is_under(k, base_root):
                v = v if isinstance(v, dict) else {}
                e = {"path": k, "trusted": bool(v.get("hasTrustDialogAccepted")),
                     "allowed_tools": len(v.get("allowedTools") or []),
                     "mcp_servers": sorted((v.get("mcpServers") or {}).keys())}
                entries.append(e)
                say("  %s: trusted %s; always-allowed tools %d; folder-only MCP servers %s" % (
                    k, "yes" if e["trusted"] else "no", e["allowed_tools"], ", ".join(e["mcp_servers"]) or "none"))
    if not entries:
        say("  no entry for this folder")
    hist = os.path.join(config_dir(), "history.jsonl")
    hcount = 0
    if os.path.isfile(hist):
        with open(hist, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    pr = json.loads(line).get("project")
                except (ValueError, AttributeError):
                    continue
                if isinstance(pr, str) and is_under(pr, base_root):
                    hcount += 1
    say("  prompt history lines for this folder: %d (up-arrow recall stays with the old path)" % hcount)

    say("\n== Running chats and editor windows inside")
    hits = open_inside([p for p in (old, new) if p])
    for kind, pid, fo in hits:
        say("  %s, process %s: %s" % (kind, pid, fo))
    if not hits:
        say("  none")
    else:
        checks.append("open now: %s. A folder move needs them all closed before the test chat; "
                      "a chat move needs only the chats being moved closed (check-closed --chats)" % describe(hits))

    if IS_WIN and old_exists and new:
        longest, lp = 0, ""
        for dp, dn, fn in os.walk(old):
            if in_skip(dp, skip):
                dn[:] = []
                continue
            for f in fn:
                L = len(os.path.join(dp, f)) - len(old) + len(new)
                if L > longest:
                    longest, lp = L, os.path.join(dp, f)
        say("\n== Longest path after the move: %d characters" % longest)
        if longest > 259:
            stops.append("a path would be %d characters after the move, over Windows' 259-character limit: %s" % (longest, short(lp)))

    say("\n== Files that write the old path" + ("" if new else ": skipped (no --new, so no path changes)"))
    roots = [norm(x) for x in (a.search or [])] if new else []
    files = list(config_search_files(skip)) if new else []
    if os.path.isdir(base_root) and new:
        files.extend(walk_files(base_root, skip))
    for rt in roots:
        if os.path.exists(rt):
            files.extend(walk_files(rt, skip))
    if not IS_WIN:
        for d in (norm("~/.local/share/applications"), norm("~/Desktop")):
            if os.path.isdir(d):
                files.extend(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".desktop"))
    seen, uniq = set(), []
    for f in files:
        k = fold(norm(f))
        if k not in seen:
            seen.add(k)
            uniq.append(norm(f))
    target = new or old
    plan_like = {"folder_moves": [{"from": old, "to": target}], "chat_folder_renames": renames}
    pairs = all_pairs(plan_like)
    olds = [o for o, _ in pairs] + report_names
    regex = build_regex(olds)
    lit = None
    if a.also_search:
        lit = re.compile("|".join(re.escape(x) for x in a.also_search), re.I if FOLD_CASE else 0)
    hits_ = find_hits(uniq, regex, lit)
    byfile = {}
    for h in hits_:
        byfile.setdefault(h["file"], []).append(h)
    if new:
        say("  searched %d text files; %d lines in %d files" % (len(uniq), len(hits_), len(byfile)))
    for f, hs in sorted(byfile.items()):
        say("  %s" % short(f))
        for h in hs[:6]:
            say("      line %d: %s" % (h["line"], snippet(h["text"], (h["matches"] or h["literals"])[0])))
        if len(hs) > 6:
            say("      … %d more lines" % (len(hs) - 6))
    if IS_WIN and new:
        lnk = windows_shortcut_hits(base_root)
        for p in lnk:
            checks.append("shortcut names the folder (fix its target, arguments or start-in folder by hand): " + short(p))

    os.makedirs(wd, exist_ok=True)
    write_json(os.path.join(wd, "hits.json"), hits_)
    if new:
        repl = {fold(o): n for o, n in pairs}
        line_changes = []
        for h in hits_:
            if not h["matches"]:
                continue
            after = regex.sub(lambda m: repl.get(fold(m.group(0)), m.group(0)), h["text"])
            if after != h["text"]:
                line_changes.append({"file": h["file"], "line": h["line"], "before": h["text"], "after": after})
        need_words = [h for h in hits_ if h["literals"] and not h["matches"]]
        plan = {
            "version": 1,
            "description": "Move %s to %s" % (old, new),
            "created": datetime.datetime.now().isoformat(timespec="seconds"),
            "backup_dir": os.path.join(wd, "backup"),
            "closed_check": [p for p in (old, new) if os.path.exists(p)],
            "folder_moves": [] if recovery else [{"from": old, "to": new}],
            "path_changes": [{"from": old, "to": new}] if recovery else [],
            "chat_folder_renames": [],
            "chat_moves": [],
            "memory_moves": [],
            "line_changes": line_changes,
            "leave_as_is": [],
            "search_roots": roots,
            "skip": skip,
            "also_search": a.also_search or [],
            "backup_copy": [],
            "backup_fingerprint": [],
        }
        for r_ in renames:
            clash = find_chat_folder(r_["to"], names)
            if recovery and clash and fold(clash[0]) != fold(r_["from"]):
                src = os.path.join(projects_dir(), r_["from"])
                ids = chat_ids(src)
                if ids:
                    plan["chat_moves"].append({"from_chat_folder": r_["from"], "to_chat_folder": clash[0], "ids": ids})
                mem = os.path.join(src, "memory")
                notes_ = sorted(f for f in os.listdir(mem) if f.endswith(".md") and f != "MEMORY.md") if os.path.isdir(mem) else []
                if notes_:
                    plan["memory_moves"].append({"from_memory": mem, "to_memory": os.path.join(projects_dir(), clash[0], "memory"), "notes": notes_})
            else:
                plan["chat_folder_renames"].append(r_)
        write_json(os.path.join(wd, "plan.json"), plan)
        say("\n== Draft plan: %d chat-folder renames, %d folder moves, %d chat moves, %d memory moves, %d line changes" % (
            len(plan["chat_folder_renames"]), len(plan["folder_moves"]), len(plan["chat_moves"]),
            len(plan["memory_moves"]), len(line_changes)))
        if need_words:
            checks.append("%d lines name the folder by a relative path (--also-search); write their new text by hand into line_changes" % len(need_words))
    say("  work folder: " + wd)

    say("\n== Summary")
    for s in stops:
        say("STOP:  " + s)
    for c in checks:
        say("CHECK: " + c)
    for n in notes:
        say("NOTE:  " + n)
    if not (stops or checks):
        say("No stop or check items.")
    return 2 if stops else 0


def windows_shortcut_hits(folder):
    dirs = [norm("~/Desktop")]
    home = norm("~")
    for n in os.listdir(home):
        if n.lower().startswith("onedrive") and os.path.isdir(os.path.join(home, n, "Desktop")):
            dirs.append(os.path.join(home, n, "Desktop"))
    for env, rel in (("APPDATA", r"Microsoft\Windows\Start Menu"), ("PUBLIC", "Desktop"),
                     ("ProgramData", r"Microsoft\Windows\Start Menu")):
        v = os.environ.get(env)
        if v:
            dirs.append(os.path.join(v, rel))
    needles = {fold(folder), fold(folder.replace("\\", "/"))}
    out = []
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for dp, _, fn in os.walk(d):
            for f in fn:
                if not f.lower().endswith((".lnk", ".url")):
                    continue
                p = os.path.join(dp, f)
                try:
                    with open(p, "rb") as fh:
                        data = fh.read(1 << 20)
                except OSError:
                    continue
                texts = [data.decode("utf-16-le", "ignore"), data[1:].decode("utf-16-le", "ignore"),
                         data.decode("latin-1")]
                if any(n in fold(t) for t in texts for n in needles):
                    out.append(p)
    return out


# ================================================================ list-chats

def cmd_list_chats(a):
    target = a.target
    names = list_chat_folders()
    found = find_chat_folder(target, names) or find_chat_folder(chat_folder_name(target), names)
    if not found:
        print("No chat folder for " + target)
        return 1
    folder = os.path.join(projects_dir(), found[0])
    print("Chat folder: " + folder)
    rows = []
    for cid in chat_ids(folder):
        p = os.path.join(folder, cid + ".jsonl")
        rows.append((chat_started(p), cid, datetime.datetime.fromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%d"),
                     nlines(p), "yes" if os.path.isdir(os.path.join(folder, cid)) else "no",
                     chat_title(p), short(first_cwd(p) or "?")))
    rows.sort()
    print("started    | id                                   | last       | lines | folder | first working folder | title")
    for r in rows:
        print("%s | %s | %s | %5d | %-6s | %s | %s" % (r[0], r[1], r[2], r[3], r[4], r[6], r[5]))
    print("%d chats" % len(rows))
    if a.json:
        write_json(norm(a.json), [dict(zip(("started", "id", "last", "lines", "folder", "title", "first_cwd"), r)) for r in rows])
    return 0


# ================================================================ check-plan

def fill_backup_lists(plan):
    pd = projects_dir()
    copy, seen = [], set()

    def add(p):
        p = norm(p)
        if fold(p) not in seen and os.path.exists(p):
            seen.add(fold(p))
            copy.append(p)
    for r in plan["chat_folder_renames"]:
        add(os.path.join(pd, r["from"]))
    for m in plan["chat_moves"]:
        src = os.path.join(pd, m["from_chat_folder"])
        for cid in m["ids"]:
            for part in chat_parts(src, cid):
                add(os.path.join(src, part))
    for mm in plan["memory_moves"]:
        for note in mm["notes"]:
            add(os.path.join(mm["from_memory"], note))
        add(os.path.join(mm["from_memory"], "MEMORY.md"))
        add(os.path.join(mm["to_memory"], "MEMORY.md"))
    for lc in plan["line_changes"]:
        add(lc["file"])
    plan["backup_copy"] = copy
    plan["backup_fingerprint"] = [norm(fm["from"]) for fm in plan["folder_moves"]]


def validate(plan):
    """Problems (blocking) and warnings for a plan, checked against the disk before anything changes."""
    problems, warnings = [], []
    pd = projects_dir()
    names = list_chat_folders()
    bd = norm(plan["backup_dir"])
    for fm in plan["folder_moves"]:
        f, t = norm(fm["from"]), norm(fm["to"])
        if not os.path.isdir(f):
            problems.append("folder to move doesn't exist: " + f)
        if os.path.exists(t) and not same_path(f, t):
            problems.append("destination already exists: " + t)
        if is_under(t, f):
            problems.append("destination is inside the folder: " + t)
        if not os.path.isdir(os.path.dirname(t)):
            problems.append("destination's parent doesn't exist: " + os.path.dirname(t))
        if is_under(bd, f):
            problems.append("backup folder is inside a folder that moves")
    if is_under(bd, pd):
        problems.append("backup folder is inside Claude Code's projects folder; two copies of a chat there break resuming it")
    for r in plan["chat_folder_renames"]:
        if not find_chat_folder(r["from"], names):
            problems.append("chat folder to rename doesn't exist: " + r["from"])
        if find_chat_folder(r["to"], names) and fold(r["to"]) != fold(r["from"]):
            problems.append("new chat-folder name already exists: " + r["to"])
        if len(r["to"]) > MAX_NAME:
            problems.append("new chat-folder name is over %d characters: %s" % (MAX_NAME, r["to"]))
    for m in plan["chat_moves"]:
        src = os.path.join(pd, m["from_chat_folder"])
        dst = os.path.join(pd, m["to_chat_folder"])
        if not os.path.isdir(src):
            problems.append("source chat folder doesn't exist: " + m["from_chat_folder"])
            continue
        if not os.path.isdir(dst):
            warnings.append("destination chat folder doesn't exist yet and will be created: " + m["to_chat_folder"])
        for cid in m["ids"]:
            if not os.path.isfile(os.path.join(src, cid + ".jsonl")):
                problems.append("chat %s isn't in %s" % (cid, m["from_chat_folder"]))
            if os.path.isdir(dst) and chat_parts(dst, cid):
                problems.append("chat %s already exists in %s" % (cid, m["to_chat_folder"]))
    for mm in plan["memory_moves"]:
        src, dst = norm(mm["from_memory"]), norm(mm["to_memory"])
        idx = read_text(os.path.join(src, "MEMORY.md")) or ""
        for note in mm["notes"]:
            if not os.path.isfile(os.path.join(src, note)):
                problems.append("memory note missing: " + os.path.join(src, note))
            if os.path.exists(os.path.join(dst, note)):
                problems.append("a note with that name already exists in the destination: " + os.path.join(dst, note))
            if not index_lines_for(idx, note):
                warnings.append("no index line in %s links %s; it moves without one" % (short(os.path.join(src, "MEMORY.md")), note))
    seen = set()
    for lc in plan["line_changes"]:
        key = (fold(norm(lc["file"])), lc["line"])
        if key in seen:
            problems.append("two changes for line %d of %s" % (lc["line"], lc["file"]))
        seen.add(key)
        text = read_text(lc["file"])
        if text is None:
            problems.append("can't read as UTF-8 text: " + lc["file"])
            continue
        lines = split_lines(text)
        if lc["line"] < 1 or lc["line"] > len(lines):
            problems.append("line %d is past the end of %s" % (lc["line"], lc["file"]))
            continue
        if body(lines[lc["line"] - 1]) != lc["before"]:
            problems.append("line %d of %s no longer matches its 'before' text" % (lc["line"], lc["file"]))
        if "\n" in lc["after"] or "\r" in lc["after"]:
            problems.append("an 'after' text holds a line break (line %d of %s)" % (lc["line"], lc["file"]))
        if lc["after"] == lc["before"]:
            warnings.append("line %d of %s doesn't change" % (lc["line"], lc["file"]))
    leave = {fold(norm(x["file"])) for x in plan["leave_as_is"]}
    for lc in plan["line_changes"]:
        if fold(norm(lc["file"])) in leave:
            problems.append("file is both changed and left as is: " + lc["file"])
    return problems, warnings


def render_change_list(plan, plan_path):
    out = ["# Change list: " + plan.get("description", ""), "",
           "**Plan file:** `%s`  " % plan_path,
           "**Written:** %s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "", "## Moves", "",
           "| What | From | To |", "|---|---|---|"]
    for fm in plan["folder_moves"]:
        out.append("| Folder | `%s` | `%s` |" % (fm["from"], fm["to"]))
    for r in plan["chat_folder_renames"]:
        out.append("| Chat folder (chats and memory) | `%s` | `%s` |" % (r["from"], r["to"]))
    for m in plan["chat_moves"]:
        out.append("| %d chats | `%s` | `%s` |" % (len(m["ids"]), m["from_chat_folder"], m["to_chat_folder"]))
    for mm in plan["memory_moves"]:
        out.append("| %d memory notes | `%s` | `%s` |" % (len(mm["notes"]), mm["from_memory"], mm["to_memory"]))
    if plan["chat_moves"]:
        out += ["", "## Chats that move", ""]
        for m in plan["chat_moves"]:
            src = os.path.join(projects_dir(), m["from_chat_folder"])
            for cid in m["ids"]:
                p = os.path.join(src, cid + ".jsonl")
                t = chat_title(p) if os.path.isfile(p) else "(missing)"
                out.append("- `%s` %s" % (cid, t))
    if plan["memory_moves"]:
        out += ["", "## Memory notes that move", ""]
        for mm in plan["memory_moves"]:
            for n in mm["notes"]:
                out.append("- `%s`" % n)
    files = {}
    for lc in plan["line_changes"]:
        files.setdefault(lc["file"], []).append(lc)
    out += ["", "## Changed lines (%d in %d files)" % (len(plan["line_changes"]), len(files)), "",
            "Each line changes only if it still reads exactly as shown under Before."]
    for f, lcs in sorted(files.items()):
        out += ["", "### `%s`" % f]
        for lc in sorted(lcs, key=lambda x: x["line"]):
            b, a_ = diff_window(lc["before"], lc["after"])
            out += ["", "Line %d" % lc["line"], "", "> Before: " + b, ">", "> After: " + a_]
    if plan["leave_as_is"]:
        out += ["", "## Left as they are", ""]
        for x in plan["leave_as_is"]:
            out.append("- `%s`: %s" % (x["file"], x.get("reason", "")))
    out += ["", "## Backup", "", "- Copied, with fingerprints: %d items" % len(plan["backup_copy"]),
            "- Fingerprinted only (a rename doesn't change their contents): %d folder(s)" % len(plan["backup_fingerprint"]),
            "- Backup folder: `%s`" % plan["backup_dir"], ""]
    return "\n".join(out)


def cmd_check_plan(a):
    pp = norm(a.plan)
    plan = load_plan(pp)
    plan["backup_dir"] = norm(plan.get("backup_dir") or os.path.join(os.path.dirname(pp), "backup"))
    fill_backup_lists(plan)
    if not plan["closed_check"]:
        plan["closed_check"] = [norm(fm["from"]) for fm in plan["folder_moves"]]
    problems, warnings = validate(plan)
    write_json(pp, plan)
    out = norm(a.out) if a.out else os.path.join(os.path.dirname(pp), "changes.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(render_change_list(plan, pp))
    print("Change list written: " + out)
    print("Moves: %d folder(s), %d chat folder(s), %d chat(s), %d memory note(s); %d changed line(s); %d file(s) left as they are" % (
        len(plan["folder_moves"]), len(plan["chat_folder_renames"]), sum(len(m["ids"]) for m in plan["chat_moves"]),
        sum(len(m["notes"]) for m in plan["memory_moves"]), len(plan["line_changes"]), len(plan["leave_as_is"])))
    for w in warnings:
        print("WARN: " + w)
    for p in problems:
        print("FAIL: " + p)
    print("PLAN OK" if not problems else "PLAN HAS PROBLEMS")
    return 1 if problems else 0


# ================================================================ check-closed

def cmd_check_closed(a):
    paths = [norm(p) for p in a.paths]
    if not paths and not a.chats:
        stop("give folders, or --chats with chat IDs")
    hits = open_inside(paths) + open_chats(a.chats or [])
    for kind, pid, fo in hits:
        print("OPEN: %s, process %s: %s" % (kind, pid, fo))
    print("NOTHING OPEN" if not hits else "SOMETHING IS OPEN")
    return 1 if hits else 0


# ================================================================ backup

def cmd_backup(a):
    pp = norm(a.plan)
    plan = load_plan(pp)
    bd = norm(plan["backup_dir"])
    problems, _ = validate(plan)
    if problems:
        stop("the plan has problems; run check-plan: " + problems[0])
    if os.path.isdir(bd) and os.listdir(bd):
        stop("backup folder already exists and isn't empty: " + bd +
             " (if an earlier backup run stopped partway, it holds only copies: delete it and run backup again)")
    items = []
    for i, src in enumerate(plan["backup_copy"], 1):
        if os.path.isdir(src):
            items.append((src, os.path.join(bd, "files", str(i))))
        else:
            items.append((src, os.path.join(bd, "files", str(i), os.path.basename(src))))
    if IS_WIN:
        longest = 0
        for src, dst in items:
            if os.path.isdir(src):
                for dp, _, fs in os.walk(src):
                    for f in fs:
                        longest = max(longest, len(os.path.join(dst, os.path.relpath(os.path.join(dp, f), src))))
            else:
                longest = max(longest, len(dst))
        if longest > 259:
            stop("a path inside the backup would be %d characters, over Windows' 259-character limit; "
                 "run inventory again with a shorter --work-dir" % longest)
    os.makedirs(os.path.join(bd, "files"), exist_ok=True)
    rows = []
    for src, dst in items:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.isdir(src):
            shutil.copytree(src, dst)
            for dp, _, fs in os.walk(src):
                for f in fs:
                    s = os.path.join(dp, f)
                    rows.append((s, os.path.join(dst, os.path.relpath(s, src))))
        else:
            shutil.copy2(src, dst)
            rows.append((src, dst))
    bad = 0
    with open(os.path.join(bd, "manifest.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["original_path", "backup_path", "size", "sha256"])
        for s, d in rows:
            hs = sha(s)
            if hs != sha(d):
                bad += 1
                print("DIFFERS: " + s)
            w.writerow([s, d, os.path.getsize(s), hs])
    skip = [norm(x) for x in plan["skip"]]
    fcount = 0
    with open(os.path.join(bd, "fingerprints.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["folder", "relative_path", "size", "sha256"])
        for folder in plan["backup_fingerprint"]:
            for dp, dn, fs in os.walk(folder):
                if in_skip(dp, skip):
                    dn[:] = []
                    continue
                for fn in fs:
                    p = os.path.join(dp, fn)
                    w.writerow([folder, os.path.relpath(p, folder), os.path.getsize(p), sha(p)])
                    fcount += 1
    shutil.copy2(pp, os.path.join(bd, "plan.json"))
    print("copied %d files in %d items; fingerprinted %d files in %d moving folders" % (
        len(rows), len(plan["backup_copy"]), fcount, len(plan["backup_fingerprint"])))
    if skip:
        print("private folders not read: %d (checked by existence only)" % len(skip))
    if bad:
        stop("%d copies differ from their originals" % bad)
    with open(os.path.join(bd, "backup-ok"), "w", encoding="utf-8") as f:
        f.write(sha(pp))
    print("BACKUP OK: all %d copies identical. Backup folder: %s" % (len(rows), bd))
    return 0


def require_backup(pp, plan):
    bd = norm(plan["backup_dir"])
    ok = os.path.join(bd, "backup-ok")
    if not os.path.isfile(ok):
        stop("no verified backup in " + bd + " (run backup first)")
    with open(ok, encoding="utf-8") as f:
        if f.read().strip() != sha(pp):
            stop("the plan changed after the backup; run check-plan and backup again in a new work folder")
    return bd


# ================================================================ test-chat

def cmd_test_chat(a):
    rec_path = norm(a.record)
    if a.action == "create":
        d = norm(a.dir)
        if not os.path.isdir(d):
            stop("folder doesn't exist: " + d)
        name = chat_folder_name(d)
        found = find_chat_folder(name)
        before = set(chat_ids(os.path.join(projects_dir(), found[0]))) if found else set()
        r = run_cli(["-p", "--model", a.model, "--output-format", "json", "--settings", NO_HOOKS, TEST_PROMPT],
                    cwd=d, timeout=300)
        if r is None:
            stop("the claude command isn't available")
        try:
            out = json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            stop("claude gave no JSON result (exit %d): %s" % (r.returncode, (r.stderr or r.stdout)[-300:]))
        sid = out.get("session_id")
        found = find_chat_folder(name)
        if not found:
            stop("no chat folder named %s after the test chat; Claude Code may name folders differently here" % name)
        folder = os.path.join(projects_dir(), found[0])
        new_ids = set(chat_ids(folder)) - before
        if new_ids != {sid}:
            stop("expected exactly the new chat %s in %s, found %s" % (sid, found[0], sorted(new_ids)))
        p = os.path.join(folder, sid + ".jsonl")
        rec = {"id": sid, "created_in": d, "chat_folder": found[0], "lines": nlines(p),
               "first_line_sha256": first_line_sha(p), "parts": chat_parts(folder, sid)}
        write_json(rec_path, rec)
        print("TEST CHAT CREATED: %s in %s (%d lines). Record: %s" % (sid, found[0], rec["lines"], rec_path))
        return 0
    rec = load_json(rec_path)
    sid = rec["id"]
    if a.action == "move":
        d = norm(a.dir)
        if open_chats([sid]):
            stop("the test chat is still running: " + describe(open_chats([sid])))
        src = os.path.join(projects_dir(), rec["chat_folder"])
        dn = find_chat_folder(chat_folder_name(d))
        dst = os.path.join(projects_dir(), dn[0] if dn else chat_folder_name(d))
        if not os.path.isdir(dst):
            os.makedirs(dst)
            print("created chat folder " + dst)
        for part in chat_parts(src, sid):
            if os.path.exists(os.path.join(dst, part)):
                stop("already in the destination: " + part)
            os.rename(os.path.join(src, part), os.path.join(dst, part))
        if chat_parts(src, sid):
            stop("parts left behind in " + src)
        rec["chat_folder"] = os.path.basename(dst)
        write_json(rec_path, rec)
        print("TEST CHAT MOVED to " + dst)
        return 0
    if a.action == "check":
        d = norm(a.dir)
        found = find_chat_folder(chat_folder_name(d))
        if not found:
            stop("no chat folder for " + d)
        folder = os.path.join(projects_dir(), found[0])
        p = os.path.join(folder, sid + ".jsonl")
        if not os.path.isfile(p):
            stop("the test chat isn't in %s" % found[0])
        n0 = nlines(p)
        before = set(chat_ids(folder))
        r = run_cli(["-p", "--model", a.model, "--output-format", "json", "--settings", NO_HOOKS, "--resume", sid,
                     TEST_PROMPT_2], cwd=d, timeout=300)
        try:
            out = json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError, AttributeError):
            out = {}
        text = open(p, encoding="utf-8", errors="replace").read()
        cw = cwds_after(p, n0)
        copies = [n for n in list_chat_folders() if fold(n) != fold(found[0])
                  and os.path.exists(os.path.join(projects_dir(), n, sid + ".jsonl"))]
        res = [
            ("resumed the same chat ID", out.get("session_id") == sid),
            ("the chat file in %s grew" % found[0], nlines(p) > n0),
            ("its first line is unchanged", first_line_sha(p) == rec["first_line_sha256"]),
            ("the first message is still there", TEST_PROMPT in text),
            ("the second message is there", TEST_PROMPT_2 in text),
            ("every new line ran in %s" % d, bool(cw) and all(same_path(k, d) for k in cw)),
            ("no copy in any other chat folder", not copies),
            ("no other new chat in the chat folder", set(chat_ids(folder)) - before == set()),
        ]
        for label, ok in res:
            print(("PASS  " if ok else "FAIL  ") + label)
        rec["check_passed"] = all(ok for _, ok in res)
        rec["checked_in"] = d
        write_json(rec_path, rec)
        print("TEST CHAT PASSED" if rec["check_passed"] else "TEST CHAT FAILED")
        return 0 if rec["check_passed"] else 1
    if a.action == "remove":
        folder = os.path.join(projects_dir(), rec["chat_folder"])
        for part in chat_parts(folder, sid):
            p = os.path.join(folder, part)
            if os.path.isdir(p):
                shutil.rmtree(p)
            else:
                os.remove(p)
            print("removed " + p)
        return 0
    stop("unknown test-chat action")


# ================================================================ apply

def log_action(bd, entry):
    p = os.path.join(bd, "applied.json")
    data = load_json(p) if os.path.isfile(p) else []
    data.append(entry)
    write_json(p, data)


def rename_checked(src, dst, bd, action):
    if not os.path.exists(src):
        stop("missing before %s: %s" % (action, src))
    if os.path.exists(dst) and not same_path(src, dst):
        stop("already exists before %s: %s" % (action, dst))
    try:
        os.rename(src, dst)
    except OSError as e:
        stop("%s failed (%s: %s). Nothing was retried. On Windows this usually means a program has the folder open." % (
            action, e.__class__.__name__, e))
    if not os.path.exists(dst) or (os.path.exists(src) and not same_path(src, dst)):
        stop("%s didn't take effect: %s" % (action, src))
    log_action(bd, {"action": action, "from": src, "to": dst})
    print("ok  %s: %s -> %s" % (action, short(src), short(dst)))


def cmd_apply(a):
    pp = norm(a.plan)
    plan = load_plan(pp)
    bd = require_backup(pp, plan)
    if os.path.isfile(os.path.join(bd, "applied.json")):
        stop("this plan was already applied (fully or partly); run verify, or revert")
    problems, _ = validate(plan)
    if problems:
        stop(problems[0])
    hits = open_inside([norm(p) for p in plan["closed_check"]] + [norm(fm["from"]) for fm in plan["folder_moves"]])
    hits += open_chats([cid for m in plan["chat_moves"] for cid in m["ids"]])
    if hits:
        stop("still open: " + describe(hits))
    pd = projects_dir()
    rows = backup_of(manifest_rows(bd))
    for r in plan["chat_folder_renames"]:
        rename_checked(os.path.join(pd, find_chat_folder(r["from"])[0]), os.path.join(pd, r["to"]), bd, "chat_folder_rename")
    for fm in plan["folder_moves"]:
        rename_checked(norm(fm["from"]), norm(fm["to"]), bd, "folder_move")
    for m in plan["chat_moves"]:
        src = os.path.join(pd, m["from_chat_folder"])
        dst = os.path.join(pd, m["to_chat_folder"])
        if not os.path.isdir(dst):
            os.makedirs(dst)
            log_action(bd, {"action": "create_chat_folder", "path": dst})
            print("ok  created chat folder " + dst)
        for cid in m["ids"]:
            for part in chat_parts(src, cid):
                rename_checked(os.path.join(src, part), os.path.join(dst, part), bd, "chat_part_move")
    for mm in plan["memory_moves"]:
        src, dst = norm(mm["from_memory"]), norm(mm["to_memory"])
        if not os.path.isdir(dst):
            os.makedirs(dst)
            log_action(bd, {"action": "create_dir", "path": dst})
            print("ok  created memory folder " + short(dst))
        si, di = os.path.join(src, "MEMORY.md"), os.path.join(dst, "MEMORY.md")
        stext = read_text(si) or ""
        dtext = read_text(di) if os.path.isfile(di) else None
        slines = split_lines(stext)
        move_idx = sorted({i for n in mm["notes"] for i in index_lines_for(stext, n)})
        moving = [slines[i] for i in move_idx]
        for n in mm["notes"]:
            rename_checked(os.path.join(src, n), os.path.join(dst, n), bd, "memory_note_move")
        if moving:
            s_before = sha(si)
            keep = [l for i, l in enumerate(slines) if i not in set(move_idx)]
            with open(si, "wb") as f:
                f.write("".join(keep).encode("utf-8"))
            log_action(bd, {"action": "index_edit", "file": si, "before_sha": s_before, "after_sha": sha(si)})
            d_before = sha(di) if dtext is not None else None
            base = dtext or ""
            if base and not base.endswith("\n"):
                base += "\n"
            add = "".join(l if l.endswith("\n") else l + "\n" for l in moving)
            with open(di, "wb") as f:
                f.write((base + add).encode("utf-8"))
            log_action(bd, {"action": "index_edit", "file": di, "before_sha": d_before, "after_sha": sha(di),
                            "created": dtext is None})
            print("ok  moved %d index lines from %s to %s" % (len(moving), short(si), short(di)))
    byfile = {}
    for lc in plan["line_changes"]:
        byfile.setdefault(norm(lc["file"]), []).append(lc)
    for orig, lcs in byfile.items():
        cur = map_path(orig, plan)
        row = rows.get(fold(orig))
        if row is None:
            stop("no backup of " + orig)
        if sha(cur) != row["sha256"]:
            stop("changed since the backup: " + cur)
        lines = split_lines(read_text(cur))
        for lc in lcs:
            if body(lines[lc["line"] - 1]) != lc["before"]:
                stop("line %d of %s no longer matches" % (lc["line"], cur))
            lines[lc["line"] - 1] = lc["after"] + ending(lines[lc["line"] - 1])
        with open(cur, "wb") as f:
            f.write("".join(lines).encode("utf-8"))
        log_action(bd, {"action": "line_change_file", "file": orig, "at": cur, "before_sha": row["sha256"],
                        "after_sha": sha(cur), "lines": sorted(lc["line"] for lc in lcs)})
        print("ok  %d line(s) in %s" % (len(lcs), short(cur)))
    print("APPLIED. Next: the test chat check (folder moves), then verify.")
    return 0


# ================================================================ verify

def cmd_verify(a):
    pp = norm(a.plan)
    plan = load_plan(pp)
    bd = norm(plan["backup_dir"])
    rows = manifest_rows(bd)
    bmap = backup_of(rows)
    pd = projects_dir()
    names = list_chat_folders()
    res = []

    def check(label, ok, detail=""):
        res.append((ok, label + (": " + detail if detail and not ok else "")))
    rec = {}
    rp = os.path.join(os.path.dirname(pp), "test-chat.json")
    if not os.path.isfile(rp):
        rp = os.path.join(bd, "test-chat.json")
    if os.path.isfile(rp):
        rec = load_json(rp)
        check("test chat passed its check after the move", bool(rec.get("check_passed")))
    test_id = rec.get("id")

    for r in plan["chat_folder_renames"]:
        new = find_chat_folder(r["to"], names)
        check("chat folder %s exists" % r["to"], bool(new))
        if fold(r["from"]) != fold(r["to"]):
            check("old chat folder %s is gone and wasn't created again" % r["from"], not find_chat_folder(r["from"], names))
        if new:
            src_rows = [x for x in rows if is_under(x["original_path"], os.path.join(pd, r["from"]))]
            want = {os.path.basename(x["original_path"])[:-6] for x in src_rows
                    if x["original_path"].endswith(".jsonl") and UUID_RE.match(os.path.basename(x["original_path"])[:-6])
                    and same_path(os.path.dirname(x["original_path"]), os.path.join(pd, r["from"]))}
            want -= {test_id} if test_id else set()
            have = set(chat_ids(os.path.join(pd, new[0]))) - ({test_id} if test_id else set())
            check("%s holds the same %d chats" % (r["to"], len(want)), have == want,
                  "missing %d, extra %d" % (len(want - have), len(have - want)))
    for fm in plan["folder_moves"]:
        f, t = norm(fm["from"]), norm(fm["to"])
        check("old folder is gone: " + short(f), not os.path.exists(f))
        check("new folder exists: " + short(t), os.path.isdir(t))
    fpp = os.path.join(bd, "fingerprints.csv")
    changed = {fold(norm(lc["file"])) for lc in plan["line_changes"]}
    if os.path.isfile(fpp):
        with open(fpp, encoding="utf-8", newline="") as fh:
            frows = list(csv.DictReader(fh))
        bad, extra = [], []
        for folder in {x["folder"] for x in frows}:
            fm = next((m for m in plan["folder_moves"] if same_path(m["from"], folder)), None)
            if not fm:
                continue
            listed = {fold(x["relative_path"]) for x in frows if x["folder"] == folder}
            for x in frows:
                if x["folder"] != folder:
                    continue
                orig = os.path.join(folder, x["relative_path"])
                if fold(norm(orig)) in changed:
                    continue
                p = os.path.join(norm(fm["to"]), x["relative_path"])
                if not os.path.isfile(p) or sha(p) != x["sha256"]:
                    bad.append(x["relative_path"])
            skip = [norm(s) for s in plan["skip"]]
            for dp, dn, fs in os.walk(norm(fm["to"])):
                mapped_skip = [map_path(s, plan) for s in skip]
                if in_skip(dp, mapped_skip):
                    dn[:] = []
                    continue
                for fn in fs:
                    rel = os.path.relpath(os.path.join(dp, fn), norm(fm["to"]))
                    if fold(rel) not in listed:
                        extra.append(rel)
        check("every moved file matches its fingerprint", not bad, "%d differ: %s" % (len(bad), ", ".join(bad[:10])))
        check("no extra files appeared in the moved folder", not extra, "%d extra: %s" % (len(extra), ", ".join(extra[:10])))
        for s in plan["skip"]:
            if any(is_under(s, m["from"]) for m in plan["folder_moves"]):
                check("private folder arrived (checked by existence only): " + short(map_path(s, plan)),
                      os.path.isdir(map_path(s, plan)) and not os.path.exists(norm(s)))
    for m in plan["chat_moves"]:
        src, dst = os.path.join(pd, m["from_chat_folder"]), os.path.join(pd, m["to_chat_folder"])
        for cid in m["ids"]:
            p = os.path.join(dst, cid + ".jsonl")
            row = bmap.get(fold(norm(os.path.join(src, cid + ".jsonl"))))
            ok = os.path.isfile(p) and row is not None and (sha(p) == row["sha256"] or open(p, "rb").read().startswith(open(row["backup_path"], "rb").read()))
            check("chat %s is in %s, unchanged or only grown" % (cid[:8], m["to_chat_folder"]), ok)
            check("chat %s left nothing in %s" % (cid[:8], m["from_chat_folder"]), not chat_parts(src, cid))
            copies = [n for n in names if fold(n) != fold(m["to_chat_folder"]) and os.path.exists(os.path.join(pd, n, cid + ".jsonl"))]
            check("chat %s has no copy elsewhere" % cid[:8], not copies, ", ".join(copies))
    for mm in plan["memory_moves"]:
        src, dst = norm(mm["from_memory"]), norm(mm["to_memory"])
        di = read_text(os.path.join(dst, "MEMORY.md")) or ""
        si = read_text(os.path.join(src, "MEMORY.md")) or ""
        for n in mm["notes"]:
            row = bmap.get(fold(norm(os.path.join(src, n))))
            p = os.path.join(dst, n)
            check("note %s moved unchanged" % n, os.path.isfile(p) and row is not None and sha(p) == row["sha256"]
                  or any(fold(norm(lc["file"])) == fold(norm(os.path.join(src, n))) for lc in plan["line_changes"]))
            check("note %s left nothing behind" % n, not os.path.exists(os.path.join(src, n)))
            check("note %s: destination index links it, source index doesn't" % n,
                  bool(index_lines_for(di, n)) and not index_lines_for(si, n) or not index_lines_for(
                      read_text(bmap.get(fold(norm(os.path.join(src, "MEMORY.md"))), {}).get("backup_path", "")) or "", n))
    byfile = {}
    for lc in plan["line_changes"]:
        byfile.setdefault(norm(lc["file"]), []).append(lc)
    for orig, lcs in byfile.items():
        cur = map_path(orig, plan)
        row = bmap.get(fold(orig))
        new_t, old_t = read_text(cur), read_text(row["backup_path"]) if row else None
        if new_t is None or old_t is None:
            check("changed file readable: " + short(cur), False)
            continue
        nl, ol = split_lines(new_t), split_lines(old_t)
        diff = [i + 1 for i in range(max(len(nl), len(ol))) if i >= len(nl) or i >= len(ol) or nl[i] != ol[i]]
        want = sorted(lc["line"] for lc in lcs)
        ok = diff == want and len(nl) == len(ol) and all(body(nl[lc["line"] - 1]) == lc["after"] for lc in lcs)
        check("%s differs from its backup only in lines %s" % (short(cur), want), ok, "differs in %s" % diff[:10])
        if cur.lower().endswith(".json"):
            try:
                json.loads(new_t)
                check("JSON still parses: " + short(cur), True)
            except ValueError:
                check("JSON still parses: " + short(cur), False)
    pairs = all_pairs(plan)
    new_regex = build_regex([n for _, n in pairs if n])
    missing, partial = [], []
    if new_regex:
        for lc in plan["line_changes"]:
            for m in new_regex.finditer(lc["after"]):
                tail = re.match(r"[^`\"'<>|*?\r\n]*", lc["after"][m.end():]).group(0)
                cand = to_native((m.group(0) + tail).rstrip(" .,;:)]"))
                if os.path.isabs(cand) or IS_WIN and re.match(r"^[A-Za-z]:", cand):
                    full, cut = cand, cand
                    while cut and not os.path.exists(cut):
                        k = max(cut.rfind(" "), cut.rfind(","), cut.rfind(";"), cut.rfind("("))
                        if k <= 3:
                            cut = ""
                            break
                        cut = cut[:k].rstrip(" .,;:)")
                    if not cut:
                        missing.append(full)
                    elif not same_path(cut, full):
                        partial.append((full, cut))
    check("every new path written exists", not missing, "; ".join(short(x) for x in missing[:5]))
    for full, cut in partial[:10]:
        print("NOTE  path exists up to %s; the rest of the line is prose or a name: %s" % (short(cut), short(full)))
    old_regex = build_regex([o for o, _ in pairs])
    if old_regex:
        files = list(config_search_files([map_path(s, plan) for s in plan["skip"]]))
        for fm in plan["folder_moves"] + plan.get("path_changes", []):
            if os.path.isdir(norm(fm["to"])):
                files.extend(walk_files(norm(fm["to"]), [map_path(s, plan) for s in plan["skip"]]))
        for rt in plan["search_roots"]:
            rt = map_path(rt, plan)
            if os.path.exists(rt):
                files.extend(walk_files(rt, [map_path(s, plan) for s in plan["skip"]]))
        leave = {fold(map_path(x["file"], plan)) for x in plan["leave_as_is"]}
        allowed = {(fold(map_path(lc["file"], plan)), lc["line"]) for lc in plan["line_changes"] if old_regex.search(lc["after"])}
        seen, left = set(), []
        for h in find_hits(files, old_regex):
            k = fold(norm(h["file"]))
            if (k, h["line"]) in seen:
                continue
            seen.add((k, h["line"]))
            if k in leave or (k, h["line"]) in allowed:
                continue
            left.append("%s line %d" % (short(h["file"]), h["line"]))
        check("the old path is left only where the plan says", not left, "; ".join(left[:8]))
    if purge_cmd():
        for fm in plan["folder_moves"] + plan.get("path_changes", []):
            r = purge_dry_run(fm["from"])
            base = fold(chat_folder_name(fm["from"]))
            left = [n for n in list_chat_folders() if fold(n) == base or fold(n).startswith(base + "-")]
            empty = [n for n in left if folder_stats(os.path.join(pd, n))["chats"] == 0
                     and folder_stats(os.path.join(pd, n))["memory_notes"] == 0]
            if r is not None and r.returncode == 0 and left and len(empty) == len(left):
                print("NOTE  after the merge, the old chat folder(s) hold no chats and no notes: %s. "
                      "Offer to delete them; delete only with the user's yes." % ", ".join(left))
                check("nothing but empty chat folders is left at the old path", True)
            else:
                check("claude purge --dry-run finds nothing at the old path", r is not None and r.returncode == 1)
            if plan["chat_folder_renames"]:
                r = purge_dry_run(fm["to"])
                want = chat_folder_name(fm["to"])
                check("claude purge --dry-run finds the chat folder at the new path",
                      r is not None and r.returncode == 0 and fold(want) in fold(r.stdout))
    else:
        print("NOTE  claude purge isn't available; that check was skipped")
    for ok, label in res:
        print(("PASS  " if ok else "FAIL  ") + label)
    allok = all(ok for ok, _ in res)
    print("ALL PASSED" if allok else "SOMETHING FAILED")
    return 0 if allok else 1


# ================================================================ revert

def cmd_revert(a):
    pp = norm(a.plan)
    plan = load_plan(pp)
    bd = norm(plan["backup_dir"])
    ap = os.path.join(bd, "applied.json")
    if not os.path.isfile(ap):
        stop("nothing applied: no " + ap)
    applied = load_json(ap)
    rows = manifest_rows(bd)
    bmap = backup_of(rows)
    paths = [norm(p) for p in plan["closed_check"]] + [norm(fm["to"]) for fm in plan["folder_moves"]]
    hits = open_inside(paths)
    if hits:
        stop("still open: " + describe(hits))
    done = []
    for e in reversed(applied):
        act = e["action"]
        if act == "line_change_file":
            cur = e["at"]
            if sha(cur) != e["after_sha"]:
                stop("changed since the plan was applied, so it isn't restored automatically: " + cur)
            shutil.copy2(bmap[fold(norm(e["file"]))]["backup_path"], cur)
            if sha(cur) != e["before_sha"]:
                stop("restore didn't match the backup: " + cur)
            print("ok  restored " + short(cur))
        elif act == "index_edit":
            if sha(e["file"]) != e["after_sha"]:
                stop("memory index changed since the plan was applied: " + e["file"])
            if e.get("created"):
                os.remove(e["file"])
            else:
                row = bmap.get(fold(norm(e["file"])))
                if row is None:
                    stop("no backup of " + e["file"])
                shutil.copy2(row["backup_path"], e["file"])
            print("ok  restored index " + short(e["file"]))
        elif act in ("chat_folder_rename", "folder_move", "chat_part_move", "memory_note_move"):
            if os.path.exists(e["from"]) and not same_path(e["from"], e["to"]):
                stop("something already exists at the original place: " + e["from"])
            try:
                os.rename(e["to"], e["from"])
            except OSError as ex:
                stop("couldn't move back %s (%s); stopped here" % (e["to"], ex))
            print("ok  moved back %s -> %s" % (short(e["to"]), short(e["from"])))
        elif act in ("create_chat_folder", "create_dir"):
            try:
                os.rmdir(e["path"])
                print("ok  removed the empty folder " + short(e["path"]))
            except OSError:
                print("NOTE  left %s in place: it isn't empty" % short(e["path"]))
        done.append(e)
    os.rename(ap, os.path.join(bd, "applied-reverted-%s.json" % stamp()))
    bad = 0
    for r in rows:
        p = r["original_path"]
        if not os.path.isfile(p):
            bad += 1
            print("MISSING  " + p)
        elif sha(p) != r["sha256"]:
            if p.endswith(".jsonl") and open(p, "rb").read().startswith(open(r["backup_path"], "rb").read()):
                print("GREW     " + short(p) + " (used since the backup; the backup is an exact prefix)")
            else:
                bad += 1
                print("DIFFERS  " + p)
    print("REVERTED and checked against the backup" if not bad else "REVERTED WITH %d PROBLEMS" % bad)
    return 0 if not bad else 1


# ================================================================ prefix-check

def cmd_prefix_check(a):
    pp = norm(a.plan)
    plan = load_plan(pp)
    bd = norm(plan["backup_dir"])
    applied = os.path.isfile(os.path.join(bd, "applied.json"))  # absent after revert: chats are back in place
    problems = grew = same = 0
    for r in manifest_rows(bd):
        orig = r["original_path"]
        if not orig.endswith(".jsonl"):
            continue
        cur = map_path(orig, plan) if applied else norm(orig)
        if not os.path.isfile(cur):
            problems += 1
            print("MISSING  " + cur)
            continue
        with open(cur, "rb") as f1, open(r["backup_path"], "rb") as f2:
            now, then = f1.read(), f2.read()
        if now == then:
            same += 1
        elif now.startswith(then):
            grew += 1
        else:
            problems += 1
            print("PROBLEM  %s doesn't start with its backup copy" % cur)
    print("chats unchanged %d, grown %d, problems %d" % (same, grew, problems))
    print("SAFE TO DELETE THE BACKUP (with the user's yes)" if not problems else "KEEP THE BACKUP")
    return 0 if not problems else 1


# ================================================================ main

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd")
    s = sp.add_parser("inventory", help="read-only: what is tied to a folder, and where its path is written")
    s.add_argument("old", help="the folder as it is now (or as it was, if it has already moved)")
    s.add_argument("--new", help="where the folder goes; also writes a draft plan")
    s.add_argument("--search", action="append", help="another folder to search for the old path (repeatable)")
    s.add_argument("--also-search", action="append", help="extra text to find, such as a relative path (repeatable)")
    s.add_argument("--skip", action="append", help="a private folder never to open, list or search (repeatable)")
    s.add_argument("--work-dir", help="where to write plan.json and hits.json (default: a new folder under ~/claude-move-backups)")
    s = sp.add_parser("list-chats", help="read-only: the chats in one chat folder")
    s.add_argument("target", help="a working folder path or a chat-folder name")
    s.add_argument("--json", help="also write the list to this JSON file")
    s = sp.add_parser("check-plan", help="check a plan against the disk and write the change list")
    s.add_argument("plan")
    s.add_argument("--out", help="change list file (default: changes.md next to the plan)")
    s = sp.add_parser("check-closed", help="read-only: running chats and editor windows inside these folders")
    s.add_argument("paths", nargs="*", help="folder move or memory move: every folder involved")
    s.add_argument("--chats", nargs="+", help="chat move: only these chat IDs must be closed")
    s = sp.add_parser("backup", help="copy what the plan changes, with fingerprints, and verify the copy")
    s.add_argument("plan")
    s = sp.add_parser("test-chat", help="throwaway chat: create, move, check or remove")
    s.add_argument("action", choices=["create", "move", "check", "remove"])
    s.add_argument("--record", required=True, help="the test chat's record file (keep it in the backup folder)")
    s.add_argument("--dir", help="create: the folder to start it in; move/check: the folder it moves to")
    s.add_argument("--model", default="haiku", help="model for the two tiny headless messages (default: haiku)")
    s = sp.add_parser("apply", help="carry out the plan, stopping at the first problem")
    s.add_argument("plan")
    s = sp.add_parser("verify", help="check the result of an applied plan")
    s.add_argument("plan")
    s = sp.add_parser("revert", help="undo an applied plan from its backup")
    s.add_argument("plan")
    s = sp.add_parser("prefix-check", help="before deleting a backup: did every backed-up chat only grow?")
    s.add_argument("plan")
    a = p.parse_args(argv)
    if not a.cmd:
        p.print_help()
        return 1
    if a.cmd == "test-chat" and a.action in ("create", "move", "check") and not a.dir:
        p.error("test-chat %s needs --dir" % a.action)
    fn = {"inventory": cmd_inventory, "list-chats": cmd_list_chats, "check-plan": cmd_check_plan,
          "check-closed": cmd_check_closed, "backup": cmd_backup, "test-chat": cmd_test_chat, "apply": cmd_apply,
          "verify": cmd_verify, "revert": cmd_revert, "prefix-check": cmd_prefix_check}[a.cmd]
    try:
        return fn(a)
    except Stop as e:
        print("STOP: " + str(e))
        return 2
    except OSError as e:
        print("STOP: %s: %s. Nothing was retried; files already handled are listed above." % (e.__class__.__name__, e))
        return 2


if __name__ == "__main__":
    sys.exit(main())
