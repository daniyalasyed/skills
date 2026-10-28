# macOS and Linux

Not tested on these systems. Everything below is from Anthropic's docs **[docs]** or inferred **[reasoned]**; tell the user so before step 1, and treat the test chat and `verify` as the proof.

- **Python:** `python3`.
- **Chat-folder names [docs]:** `/home/ana/work/my-app` becomes `-home-ana-work-my-app`; on macOS `/Users/ana/my app` becomes `-Users-ana-my-app`.
- **Letter case [reasoned]:** macOS's default file system ignores case, like Windows; Linux file systems don't, so `My-App` and `my-app` are two folders there. The script compares names without case on macOS and Windows, and exactly on Linux.
- **Folders in use [reasoned]:** macOS and Linux let a folder be renamed while a program has it open; the program keeps working on the moved folder. A running chat could then keep writing to the old chat folder name and split the chats in two. So run `check-closed` before every `apply` and `revert`: here nothing else stops a move while a chat is open, whereas on Windows the rename itself fails.
- **Running-chat check [docs, Python]:** `check-closed` asks the system with `os.kill(pid, 0)`, which only checks that a process exists.
- **Path spellings in files:** `/home/ana/…`, `~/…` and `$HOME/…`. The inventory finds all three.
- **Links:** a symbolic link at the old path is exactly what this skill avoids (`known-issues.md`). If the user already has one, the inventory reports the folder as a link and stops.
- **Launchers:** on Linux, `.desktop` files in `~/.local/share/applications` and `~/Desktop` are searched like other text files. macOS Finder aliases are binary and aren't searched; ask the user whether any alias or Dock item points into the folder.
- **Trust [docs]:** the terminal may ask the trust question the first time Claude Code starts in the moved folder; answer it once.
- **Moving across disks [reasoned]:** `os.rename` fails across file systems (for example to an external drive), and the inventory stops before that happens.
