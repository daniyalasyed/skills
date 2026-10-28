# How Claude Code ties chats and memory to a folder

Every fact here carries its source:

- **[docs]** Anthropic's Claude Code documentation (pages: sessions, `.claude` directory, memory, settings, permissions, CLI reference), checked in October 2026.
- **[tested]** observed on Windows 11 with the VS Code extension and the terminal, Claude Code 2.1.289, while moving several folders with hundreds of chats.
- **[reported]** a public bug report (`known-issues.md`), not confirmed by Anthropic.
- **[reasoned]** inferred from the above, not tested.

## Contents

1. Where chats live
2. Where memory lives
3. What else is filed by path
4. Resuming and the chat list
5. Running chats and open windows
6. Retention
7. Settings that move with the folder

## 1. Where chats live

- **[docs]** Chats are stored at `<config folder>/projects/<name>/<id>.jsonl`. `<name>` is the working folder's full path with every non-alphanumeric character replaced by `-`. A name longer than 200 characters is cut to 200 and a hash of the full path is added; the hash isn't documented, so this skill stops in that case.
- **[docs]** The config folder is `~/.claude`, or the folder `CLAUDE_CONFIG_DIR` names. `CLAUDE_CODE_PROJECT_DIR_NAME` (with `CLAUDE_CONFIG_DIR`) replaces the derived `<name>`; then chats aren't filed by path and this skill doesn't apply.
- **[tested]** The rule held for every chat started in place: 188 chats sat exactly where the rule puts them. `C:\Users\ana\my app` becomes `C--Users-ana-my-app`; a space, `\`, `:` and `'` each become `-`.
- **[tested]** The VS Code extension on Windows can write the drive letter in lowercase (`c--Users-…`) where the terminal writes `C--Users-…`. Windows treats them as one folder.
- **[docs]** A chat can own more than `<id>.jsonl`: an `<id>` folder (subagent transcripts, large tool outputs, MCP task state) and set-aside copies `<id>.orphaned-<timestamp>-<suffix>.jsonl` and `<id>.jsonl.superseded-<timestamp>`, which Claude Code keeps instead of overwriting or deleting a transcript.
- **[tested]** A chat folder can also hold folders with no matching `.jsonl` (a saved tool output from a chat that wrote nothing else). They belong to the chat folder and move with it in a folder move.
- **[docs]** The transcript format is internal and changes between versions. **[tested]** Each line records the working folder in a `cwd` field; a moved chat keeps its old `cwd` on old lines and records the new folder on lines written after the move. Claude Code finds chats by the chat folder they sit in, not by `cwd`.
- **[reported]** Different folders can share one name: `_`, `-`, `/` and spaces all become `-`, so `my_app`, `my-app`, `my app` and `my/app` collide. The inventory stops when the new name is already taken.

## 2. Where memory lives

- **[docs]** Auto memory is in `<chat folder>/memory/`. `MEMORY.md` is its index; the first 200 lines or 25 KB load at the start of every chat.
- **[docs]** The memory `<name>` comes from the git repository root, not the working folder: every subfolder and worktree of one repository shares the root's memory. Outside a repository it is the working folder's own.
- **[reported]** Chats started in a repository subfolder are still filed under the subfolder's chat folder while their memory comes from the root's (issue #85595). A folder move inside one repository therefore moves chats but not memory; a move into or out of a repository changes which memory the folder's chats read.
- **[docs]** `autoMemoryDirectory` in a settings file points memory elsewhere; that memory doesn't follow a folder move.
- **[docs]** The retention sweep never deletes memory notes.
- **[reported]** A chat resumed after its memory folder changed can keep naming the old memory folder in its instructions and save notes there (issue #101500, open). After a memory move, a note saved by an old chat can land in the old folder; check for new notes there after the user's first sessions.

## 3. What else is filed by path

- **[docs]** `claude purge <path> --dry-run` lists everything Claude Code holds for one folder: the chat folder (chats and memory), per-chat file history, task lists and debug logs, the folder's lines in `history.jsonl` and its entry in `.claude.json`. It deletes nothing with `--dry-run`, and exits with status 1 and "No Claude Code project state found" when nothing matches. Before v2.1.288 the command was `claude project purge`.
- **[tested]** `claude purge <path> --dry-run` also lists the chat folders of the folder's subfolders, so after a folder move it should find nothing at the old path.
- **[tested]** After chats and notes are merged out of an old chat folder, that folder remains with an empty `MEMORY.md`, and `purge --dry-run` still lists it. `verify` reports it as an empty leftover.
- **[tested]** Starting any chat in a folder, even one that saves nothing (`claude -p --no-session-persistence`), creates that folder's `memory` directory if it is missing.
- **[tested]** After a folder move inside a git repository, a new chat started in a subfolder found a note in the repository root's memory, as the docs describe.
- **[docs]** File history (`<config folder>/file-history/<id>/`) is filed by chat ID, so it follows a chat wherever its file goes.
- **[docs]** `.claude.json` (in the home folder) keeps one entry per folder path: the trust answer, folder-only ("local scope") MCP servers and other per-folder state. A moved folder starts without them.
- **[tested]** Chats in the VS Code panel created no `.claude.json` entry and no `history.jsonl` lines; terminal chats create both [docs].
- **[docs]** Trust is filed under the git repository root, or outside a repository under the folder you started in, and covers its subfolders. Starting in the home folder keeps trust for that chat only.
- **[tested]** In VS Code no trust question appeared after a move. The one extra step: the Claude Code extension was enabled per workspace, and the moved folder counts as a new workspace.
- **[docs]** The per-chat scratchpad lives under the system temp folder, named with the same rule; `claude purge` doesn't touch it and the system may clear it.

## 4. Resuming and the chat list

- **[docs]** The chat list shows the chats of the current folder (and its worktrees). In the terminal picker, `Ctrl+A` widens it to every folder on the machine.
- **[docs]** `claude --resume <id>` searches the current folder, then every other folder on the machine (since v2.1.223). It refuses when more than one folder holds a copy of the chat, so a copied chat breaks resuming it.
- **[reasoned]** Because of that search, resuming a chat by ID can succeed even when its file never moved. A test must check that the file in the destination chat folder grew, not only that the resume worked. `test-chat check` does this.
- **[docs]** Chats made with `claude -p` never appear in any chat list. The user's own check in step 7 needs a real chat.
- **[docs]** `/cd <path>`, typed inside a running chat, moves that one chat to another folder's chat list and applies that folder's settings. It is the official way to move a single open chat; the skill's chat move does the same for many closed chats by moving their files.
- **[tested]** Moving a chat's parts into another chat folder works: the chat appears in that folder's list, resumes with its history, records the new working folder and saves new memory notes to the new folder's memory.
- **[tested]** The VS Code list shows recent chats first; older chats are found by searching their name.
- **[tested]** A chat open during the backup gains a line or two when its window closes; its backup is then an exact prefix of the file, which `verify` and `prefix-check` accept.

## 5. Running chats and open windows

- **[docs]** `<config folder>/sessions/` holds one small file per running chat, removed when it exits; crash leftovers are cleared at the next launch. **[tested]** Each holds `pid` and `cwd`.
- **[tested]** `<config folder>/ide/*.lock` has one file per editor window with the extension: `pid`, `workspaceFolders` and an `authToken`. Read only the first two.
- **[tested]** A leftover record can name a process that has ended, so `check-closed` checks each process is running.
- **[tested]** Reading any file inside a folder makes Claude Code watch that folder's project skills, which can keep the folder busy until that chat ends. Do moves from a chat that never worked inside the moving folder.
- **[tested]** Hooks run in every chat, headless ones included. A plugin's session hooks wrote log and lock files into the working folder at chat start and end, and kept working in the background for a while after the chat ended. Those files change the folder's fingerprints and can keep it busy. **[docs]** `--settings '{"disableAllHooks": true}'` switches hooks off for one run; `test-chat` uses it.

## 6. Retention

- **[docs]** `cleanupPeriodDays` (default 30, minimum 1) deletes chats, file history and task lists older than that. A move doesn't change file dates **[reasoned]**, so it neither speeds up nor delays this.

## 7. Settings that move with the folder

- **[docs]** "Yes, and don't ask again" answers are saved in the folder's `.claude/settings.local.json` (at the repository root when started in a subfolder, except on Windows), so they move with the folder.
- **[docs]** Project settings, skills, `CLAUDE.md` files and `.mcp.json` live inside the folder and move with it. Any full path written inside them is in the inventory's list of files that name the old path.
