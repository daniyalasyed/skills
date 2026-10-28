# Skills by Daniyal A. Syed

A Claude Code plugin marketplace. Each plugin here bundles one skill.

## Install

### Claude.ai

1. Turn on **Code execution and file creation** in Settings → Capabilities. Skills upload fine without it and then silently never run.
2. Download the skill zip (links under [Plugins](#plugins) below).
3. Go to **Customize → Skills → Add → Upload a skill** and drop the zip in.
4. Wait for the security scan, usually a minute or two.

### Claude Code

```
/plugin marketplace add daniyalasyed/skills
/plugin install <plugin-name>@daniyalasyed-skills
```

Update later with `/plugin marketplace update daniyalasyed-skills`.

## Plugins

### Lived Experience Miner by Matt Barker

**Download:** [lived-experience-miner-by-matt-barker.zip](https://github.com/daniyalasyed/skills/raw/main/dist/lived-experience-miner-by-matt-barker.zip)  
**Claude Code:** `/plugin install lived-experience-miner-by-matt-barker@daniyalasyed-skills`

A writing coach for the step most people skip: finding the real story before writing the post.

It interviews you. It asks one question at a time, digs when your answer is vague, and hands your own material back as notes you write from. It does not write the post, produce draft sentences, or suggest hooks — and it holds that line even when you ask it outright to just write the thing.

The test it applies to everything it finds: **"Can someone else write this?"** If yes, it keeps digging.

**When it triggers:** you're preparing a LinkedIn post, you have a topic but no story, your draft feels like anyone could have written it, or you ask for a post to be drafted from scratch.

**When it doesn't:** LinkedIn profile or headline reviews, DMs and connection requests, algorithm and timing tactics, company-page content calendars, or writing for other platforms.

### Moving Claude Project Folders

**Claude Code only.** It works on the files Claude Code keeps on your computer, so it can't run on claude.ai. The build makes a zip for it like every skill here, but that zip won't work on claude.ai.  
**Claude Code:** `/plugin install moving-claude-project-folders@daniyalasyed-skills`

Rename or move a folder you've used with Claude Code without losing its chats, memory or settings.

Claude Code files every chat, and the memory it keeps, under a name built from the full path of the folder the chat started in. Rename or move the folder and Claude Code looks in a new, empty place: the old chats disappear from the list and memory stops loading. This skill moves the folder and everything tied to it together.

It works in fixed steps: a read-only inventory, a change list you approve, a fingerprinted backup, a throwaway test chat that is moved and reopened before any real chat, the move itself, a full set of checks, and a one-command undo. It never edits chat files.

It also moves chosen chats or memory notes from one folder to another, and recovers chats left behind when a folder was moved by hand.

**Needs:** Python 3.8 or later.  
**Tested on:** Windows 11 with Claude Code 2.1.289. macOS and Linux follow Anthropic's documentation and haven't been tested; the skill says which facts come from where.

**When it triggers:** you want to rename, move or reorganize folders you use with Claude Code, move past conversations into another project, split or merge a folder's chats or memory, or your chats vanished after a folder moved.

**When it doesn't:** renaming code or moving files inside a project.

## Credit

The Lived Experience Miner method — the three depths, the time prompts, the "can someone else write this?" test, and the coaching rules — is **Matt Barker's** (<https://www.mattbarker.xyz/>). He wrote and shared it as a Claude Project prompt.

This repository packages that method as an Agent Skill so it can be installed and triggered automatically rather than pasted into project instructions. The method is his; the packaging is mine. If you find it useful, the credit belongs upstream.

## Packaging

`dist/` holds one zip per skill, rebuilt automatically by
[.github/workflows/package-skills.yml](.github/workflows/package-skills.yml) on
every push that touches a skill. Don't edit the zips by hand — change the
`SKILL.md` and push.

Download links point at `raw/main/dist/`, which is a fixed path: it always
serves the current version and never changes as skills are added or updated.
