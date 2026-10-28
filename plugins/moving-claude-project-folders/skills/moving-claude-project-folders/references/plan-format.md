# The plan file

`plan.json` lists every change. The script carries out only what it lists, in this order: chat-folder renames, folder moves, chat moves, memory moves, line changes. `inventory --new` drafts one for a folder move; write one by hand for a chat or memory move. Run `check-plan` after every edit: it fills the backup lists, checks every entry against the disk and writes the change list.

All paths are full paths. A chat-folder name is the bare name under `<config folder>/projects/`. Use one plan per move; a plan that mixes kinds of move works, but each extra kind is one more thing to check.

## Fields

| Field | What it holds |
|---|---|
| `description` | One line for the change list's title. |
| `backup_dir` | Where `backup` writes. Outside every moving folder and outside `<config folder>/projects`. |
| `closed_check` | Working folders where nothing may be open during `apply` and `revert`. For a chat or memory move: both working folders. |
| `folder_moves` | `[{"from": "/old/path", "to": "/new/path"}]`. Same drive or file system only. |
| `path_changes` | Recovery only: `[{"from": "/old/path", "to": "/new/path"}]` for a folder that already moved; used to find and check paths. |
| `chat_folder_renames` | `[{"from": "<old name>", "to": "<new name>"}]`, one per chat folder, including subfolders' and the repository root's. |
| `chat_moves` | `[{"from_chat_folder": "<name>", "to_chat_folder": "<name>", "ids": ["<chat id>", …]}]`. All parts of each chat move. A missing destination chat folder is created. |
| `memory_moves` | `[{"from_memory": "<full path of memory folder>", "to_memory": "<full path>", "notes": ["note.md", …]}]`. Each note's lines in the source `MEMORY.md` (lines that link `(note.md)`) move to the end of the destination's. |
| `line_changes` | `[{"file": "<path before any move>", "line": 12, "before": "<whole line now>", "after": "<whole line after>"}]`. `before` must match the line exactly, without its line ending; `after` holds no line break. Files are named by their path before the moves; the script finds them afterwards. |
| `leave_as_is` | `[{"file": "<path>", "reason": "dated record"}]`. Files that name the old path on purpose. `verify` allows the old path only here and in `after` texts that name it on purpose. |
| `search_roots`, `skip`, `also_search` | Copied from the inventory; `verify` searches the same places for leftovers and stays out of `skip`. |
| `backup_copy`, `backup_fingerprint` | Filled by `check-plan`: what `backup` copies, and the moving folders it fingerprints without copying (a rename doesn't change their contents). |

## Example: chat move

```json
{
  "version": 1,
  "description": "Move 3 chats about the website from ~/notes to ~/work/website",
  "backup_dir": "/home/ana/claude-move-backups/261028-101500/backup",
  "closed_check": ["/home/ana/notes", "/home/ana/work/website"],
  "chat_moves": [
    {"from_chat_folder": "-home-ana-notes", "to_chat_folder": "-home-ana-work-website",
     "ids": ["0f1e2d3c-4b5a-4968-8776-655443322110"]}
  ],
  "memory_moves": [],
  "line_changes": [],
  "leave_as_is": []
}
```

## Example: memory move

```json
{
  "version": 1,
  "description": "Move the website notes into the website folder's memory",
  "backup_dir": "/home/ana/claude-move-backups/261028-103000/backup",
  "closed_check": ["/home/ana/notes", "/home/ana/work/website"],
  "memory_moves": [
    {"from_memory": "/home/ana/.claude/projects/-home-ana-notes/memory",
     "to_memory": "/home/ana/.claude/projects/-home-ana-work-website/memory",
     "notes": ["site-launch-plan.md"]}
  ]
}
```

Before a memory move, search the source memory folder for notes that link a moving note by name, and list them in the change list: after the move, a link from a note left behind points into a folder that no longer holds the note **[reasoned]**.
