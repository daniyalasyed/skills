---
name: moving-claude-project-folders
description: Rename or move a folder you've used with Claude Code without losing its chats, memory or settings, and move chats or memory notes from one folder's history to another's. Use whenever someone wants to rename, move, reorganize or restructure folders they work in with Claude Code, move past conversations into another project, split or merge a folder's chats or memory, or get back chats and memory that vanished after a folder was moved or renamed, even if they don't mention chats or memory.
---

# Moving Claude Code project folders

Claude Code files every chat, and the memory it keeps, under a folder named after the full path of the folder the chat started in. Rename or move that folder and Claude Code looks in a new, empty place: the old chats vanish from the chat list and memory stops loading. This skill moves the folder and everything tied to it together, checks every result, and can undo all of it from a backup.

## What it moves

- **Folder move:** rename or move a working folder. Its chat folder, the chat folders of its subfolders, and its memory follow it, and full paths written in other files are updated.
- **Chat move:** move chosen chats from one folder's chat list to another's.
- **Memory move:** move chosen memory notes, with their index lines, from one memory folder to another.
- **Recovery:** the folder was already moved and its chats are gone from the list. See "Already moved" near the end.

These stay out of scope; tell the user so and stop: a move to another drive or file system, a folder that is a link or junction, a new chat-folder name over 200 characters, `CLAUDE_CODE_PROJECT_DIR_NAME` set, and cloud sessions.

## Terms

Explain each term to the user the first time you use it.

- **Working folder:** the folder a chat was started in.
- **Config folder:** `~/.claude`, or the folder `CLAUDE_CONFIG_DIR` names. On Windows `~` is `%USERPROFILE%`.
- **Chat folder:** `<config folder>/projects/<name>`, where `<name>` is the working folder's full path with every character that isn't a letter or digit turned into `-`. `/home/ana/work/my-app` gives `-home-ana-work-my-app`; `C:\Users\ana\my app` gives `C--Users-ana-my-app`.
- **Chat:** one conversation, stored in its chat folder as `<id>.jsonl`, sometimes with an `<id>` folder and set-aside copies named `<id>.orphaned-…` or `<id>.jsonl.superseded-…`. Those are its **parts**; they always move together.
- **Chat list:** what an app shows for a working folder: the chats in its chat folder.
- **Memory:** notes in `<chat folder>/memory/`, with `MEMORY.md` as the index loaded at the start of each chat. Inside a git repository every subfolder's chats use the memory of the repository root's chat folder.
- **Plan:** a JSON file listing every move and every changed line. The script carries out only what the plan lists. Format: `references/plan-format.md`.
- **Change list:** the plan written out for the user: moves as a short table, and every changed line with its text before and after.
- **Backup:** copies of everything the plan changes, with each file's SHA-256 fingerprint, kept outside the config folder and outside every folder that moves.
- **Test chat:** a throwaway chat that is moved and reopened before any real chat.

## Ground rules

Each rule answers a problem seen in practice (`references/known-issues.md`, `references/how-claude-code-stores-state.md`).

- **Run from a chat started outside every folder that moves.** A running chat keeps its folder open, and a chat can't safely move the folder it runs in. Check the start folder in your environment details. If it is inside a moving folder, tell the user to start a new chat in a folder outside it, such as their home folder, and stop.
- **Move, never copy, inside the config folder's `projects` folder.** Two copies of one chat make resuming it by ID fail.
- **Leave chat files as they are.** Their format is internal and changes between versions; moving the file is enough.
- **Leave nothing at the old path.** A link there would file new chats under the old name again.
- **Run `claude purge` only with `--dry-run`.** Without it, purge deletes.
- **Change only what the approved plan lists.** Ask before anything else.
- **Keep out of private folders.** Ask the user at the start which folders must never be opened, listed or searched, and pass each with `--skip`.
- **Read only process IDs and folders from editor lock files.** They also hold an access token.
- **Leave `.claude.json` alone.** Every running chat rewrites it. Step 9 tells the user what to set up again instead.

## The stop rule

Stop and change nothing further when any of these is true:

1. A count differs from the inventory or the plan.
2. A file or folder the plan names is missing, or one it would create already exists.
3. A rename, move, copy or edit returns an error.
4. A script prints `STOP:` or `FAIL`.
5. A permission prompt appears for something other than the step in progress.
6. You can't name the exact result a step will have.

Then tell the user four things: what you were doing, what you expected, what you found, and two or three ways to continue with one marked as recommended. Wait for their answer. Never retry a failed move and never work around a check. A slip in your own wording, or in a read-only command that changes nothing, doesn't stop the work: fix it and report it in one line.

## The script

`scripts/claude_move.py` in this skill's folder does every mechanical step. It needs Python 3.8 or later and nothing else: run it as `python3` on macOS and Linux, `python` or `py -3` on Windows. If no Python 3.8 or later is installed, tell the user it is needed and offer to help install it; don't do the steps by hand.

Commands that change nothing apart from their own report files: `inventory`, `list-chats`, `check-plan`, `check-closed`, `verify`, `prefix-check`. Commands that change things, each run only at its step: `backup`, `test-chat`, `apply`, `revert`. Every command has `--help`.

Read the reference for the user's system before step 1: `references/windows.md` or `references/macos-linux.md`.

## Steps

Finish each step, report its result to the user in a few plain lines, then go on. Steps 3 to 8 need the user's yes to the change list first.

### 1. Inventory

Ask the user four things, then run the inventory:

- Which move: folder, chats or memory notes, and from where to where.
- Which folders must never be opened.
- Whether other folders hold files that name this folder by its full path, such as a parent folder with notes or scripts. Pass each with `--search`.
- Whether documents name it by a path relative to a parent, such as `projects/my-app/`. Pass each with `--also-search`.

```
python3 <skill>/scripts/claude_move.py inventory "<folder now>" --new "<folder after>" --skip "<private>" --search "<other folder>"
```

For a chat or memory move, run `inventory` on both working folders without `--new`, then `list-chats "<working folder>"` and choose the chats with the user, by title and recorded working folder.

Read the whole report; it ends with `STOP:`, `CHECK:` and `NOTE:` lines. Every `STOP:` line falls under the stop rule. Every `CHECK:` line is a decision for the user. Note the work folder the report names: the plan, the change list and the backup live there.

Done when every `STOP:` and `CHECK:` line is settled with the user.

### 2. Change list

For a folder move, `inventory --new` drafted `plan.json` in the work folder. For a chat or memory move, write the plan by hand from `references/plan-format.md`. Then edit it:

- Move each **dated record** from `line_changes` to `leave_as_is`, with the reason. A dated record describes the past: a handoff, a log, a changelog, a dated note, a backup. It stays true as written.
- Rewrite any `after` text in a line that records history, such as "moved here from <old path>". A plain path swap would make the record false.
- Add `line_changes` for the `--also-search` hits, with the new wording written out.
- For chat and memory moves, add both working folders to `closed_check`.

Then run `check-plan "<work folder>/plan.json"`. It writes `changes.md` and prints `PLAN OK` or each `FAIL`. Show the user the moves as a short old-to-new table in the chat, and give them the path to `changes.md` for the line-by-line detail. They approve the decisions; they don't need to read every line.

Done when `check-plan` prints `PLAN OK` and the user said yes to the change list.

### 3. Test chat

Skip this step when the inventory found no chat folder to move. Otherwise tell the user it sends two one-line messages on a small model, then run the test chat before the backup, so it is in the backup and anything it started has finished before the move. Keep its record in the work folder: `--record "<work folder>/test-chat.json"`.

- **Folder move:** `test-chat create --dir "<folder now>"`. The test chat moves with its chat folder in step 6 and is checked right after it.
- **Chat move:** `test-chat create` in the source folder, `test-chat move --dir "<destination folder>"`, then `test-chat check --dir "<destination folder>"`.

The script runs test chats with hooks switched off for that run, because a plugin's session hook can write into the folder and keep it busy after the chat ends. Tell the user: open no real chat in the new place until the test chat passes. A chat made this way never appears in a chat list (Claude Code hides headless chats), so the script does its check.

Done when `test-chat create` printed `TEST CHAT CREATED`, and for a chat move `test-chat check` printed `TEST CHAT PASSED`.

### 4. Backup

```
python3 <skill>/scripts/claude_move.py backup "<work folder>/plan.json"
```

It copies every chat folder, chat, memory note and file the plan changes, fingerprints the folders that move, and checks every copy. A chat folder can hold over a gigabyte: before running it, tell the user the space the backup needs, from the chat-folder sizes in the inventory. Done when it prints `BACKUP OK`.

### 5. Nothing open

Ask the user to close every chat, terminal, editor window and file browser window that has a moving folder, or a folder inside one, open. In VS Code, tell them to open folders only through File → New Window, then Open Folder in the new window. Open Folder in the window running this chat would end this chat.

```
python3 <skill>/scripts/claude_move.py check-closed "<folder>" ["<folder>" …]
```

Done when it prints `NOTHING OPEN`. Run it again right before step 6 if anything happened in between.

### 6. Change

```
python3 <skill>/scripts/claude_move.py apply "<work folder>/plan.json"
```

It renames chat folders first, then moves folders, chats and notes, then edits the approved lines, each only if it still matches the backup. It stops at the first problem and records every action, so `revert` can undo a move that stopped halfway. For a folder move, run `test-chat check --dir "<folder after>" --record "<work folder>/test-chat.json"` straight after.

If `apply` stops on "Access is denied" or "in use", something still has the folder open; on Windows, Resource Monitor finds it (`references/windows.md`). Run `revert`. Once the user has closed that program and said yes, run step 5 again and then `apply` once more.

Done when `apply` printed `APPLIED` and, for a folder move, the test chat printed `TEST CHAT PASSED`.

### 7. Check

```
python3 <skill>/scripts/claude_move.py verify "<work folder>/plan.json"
```

Then ask the user to check in their app, opening the moved folder in a new window:

- **VS Code:** if the Claude Code extension is enabled per workspace, enable it for the moved folder (Extensions → Claude Code → Enable (Workspace)). Open the past-conversations list: the moved chats appear. The list shows recent chats first; search an older chat by name. Open one old chat and see its history.
- **Terminal:** in the moved folder, run `claude --resume`; the picker lists the moved chats.
- **JetBrains or the desktop app:** the app's session list for the moved folder.

When `verify` lists files that differ or appeared inside the moved folder, show the list to the user. Files inside a plugin's own folder in the project (for example a log folder a session hook writes) can change on their own; anything else falls under the stop rule.

Done when `verify` prints `ALL PASSED`, or every failed line is explained to the user and they accepted it, and the user has seen the moved chats in the list.

### 8. Keep or undo

The user decides. To undo, run step 5 again, then:

```
python3 <skill>/scripts/claude_move.py revert "<work folder>/plan.json"
```

Done when the user said keep, or `revert` printed `REVERTED and checked against the backup`.

### 9. Afterwards

Tell the user what to set up again, from the inventory's `.claude.json` section:

- **Terminal:** Claude Code may ask the trust question the first time it starts in the moved folder.
- **VS Code:** the extension may need enabling for the moved folder, as in step 7.
- **Folder-only MCP servers** the inventory listed: add each again from the moved folder with `claude mcp add --scope local`.
- **Prompt history:** up-arrow recall of prompts typed in the old folder stays with the old path.
- **Retention:** if the inventory noted that `cleanupPeriodDays` isn't set, Claude Code deletes chats older than 30 days. Say this once; a move doesn't change it.

Keep the backup until the user has worked normally in the new place in at least one chat. Then run `prefix-check "<work folder>/plan.json"`. When it prints `SAFE TO DELETE THE BACKUP`, ask the user, and with their yes run `test-chat remove --record "<work folder>/test-chat.json"` and delete the work folder.

## Already moved

When a folder was moved before this skill ran, its chats still sit in the chat folder named after the old path. If someone opened the new path since, Claude Code also made a new, nearly empty chat folder for it.

1. Ask the user to open no old chat from the new place for now. A reported bug (`references/known-issues.md`) says an old chat opened there loaded blank and its file was overwritten. Current Claude Code says it sets such a file aside instead, but nobody has confirmed that.
2. Run `inventory "<old path>" --new "<current path>"`. It notices that only the new path exists and drafts a plan that renames the old chat folders. Where a new chat folder already exists, it moves the old chats and notes into it instead.
3. Continue from step 2. There is no folder to move, so skip the test chat when the plan only renames chat folders.
4. After a merge, the old chat folder is left holding no chats and no notes, only an empty index. `verify` names it; delete it only with the user's yes.

## References

- `references/how-claude-code-stores-state.md`: every fact this skill relies on, each marked by source. Read it when a result surprises you or the user asks why.
- `references/known-issues.md`: the public bug reports behind the ground rules.
- `references/plan-format.md`: the plan's fields. Read it before editing a plan by hand.
- `references/windows.md` and `references/macos-linux.md`: what differs on each system.
