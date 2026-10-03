# AI-Verse Social

**Your AI social media assistant, from the AI-Verse community.**

Give your agent a video—or keep videos in Google Drive. Ask it to prepare, publish or schedule them. Speak normally; there are no magic sentences to memorise.

## Install in one line

Mac, Linux or Windows with WSL/Git Bash. Requires Git and Python 3.11+; the installer explains missing prerequisites.

```bash
curl -fsSL https://raw.githubusercontent.com/aiverse-filmmakers/ai-verse-social/main/install.sh | bash
```

1. Choose **Hermes, Claude Code, Codex or OpenClaw**.
2. Reload your agent.
3. Say **“Set up AI-Verse Social.”**

Setup checks what is missing and walks you through Drive, Zernio, your connected social accounts, captions and posting preferences. FFmpeg is needed for local media processing. Installation does not connect accounts, publish anything or start scheduled jobs.

### Private repository

Members need GitHub access to this repository and an authenticated GitHub CLI (`gh auth login`). Then paste this single line:

```bash
( d=$(mktemp -d); trap 'rm -rf -- "$d"' EXIT; gh repo clone aiverse-filmmakers/ai-verse-social "$d/repo" -- --depth 1 && bash "$d/repo/install.sh" )
```

The public download command cannot fetch a private repository anonymously.

## Remember four jobs

**Prepare → Choose → Publish → Remember**

1. **Prepare:** Keep originals safe, organise videos and apply supported edits/branding. Use transcripts or your context to write captions for each destination.
2. **Choose:** Use a video you send, a Drive link or your saved library. Select particular accounts or your configured “all”.
3. **Publish:** Post now, plan upcoming times, or prepare recurring posting with your agent’s scheduler. Live requests use the same tracking as scheduled work.
4. **Remember:** Track each account separately, avoid repeating successful posts, recover partial failures and learn from caption corrections. Newly connected accounts can use earlier videos when you enable that policy.

Try: “Post this video on my selected accounts tomorrow.” Or: “What still needs posting?” These are examples, not required wording.

Optional comment/DM setup gathers your rules and checks for an available engagement integration. **This package’s local engine does not run a comment or DM worker.** Website intake also requires a separate source integration.

**AI-Verse Studio** is reserved for a future companion editing skill; it is not included here.

## Setup, updates and details

- Short setup alias: `/ai-verse-social-onboard` where supported. Natural-language setup always works when the skill is loaded.
- [Simple feature guide](ai-verse-social-media/START-HERE.md)
- [Dependencies and installation details](ai-verse-social-media/INSTALL.md)
- [Verified features and integration boundaries](ai-verse-social-media/FEATURE-MATRIX.md)
- [Verification results](ai-verse-social-media/VERIFICATION.md)
- [Reuse terms](ai-verse-social-media/REUSE.md)

Agent commands and unattended scheduling vary by host. These are portable skill files and an executable engine, not certification of every host version. Live customer-account tests still happen during setup.

For a specific agent, append `-s -- --host hermes` (or `claude-code`, `codex`, `openclaw`) to the public command’s `bash` invocation. For custom profiles, the installer respects `HERMES_HOME` and `OPENCLAW_STATE_DIR`; `--skills-dir /your/path` overrides the destination.

To update, back up your private workspace and pause scheduled work first. Re-run with `--update`; unchanged installer-owned skills are replaced with a backup. Personal edits cause installation to stop for review. Account state and secrets live outside the installed skill and are preserved. Run setup/audit before resuming.

The repository contains only the distributable package. Original school captures, account credentials, videos and customer publication logs are excluded.
