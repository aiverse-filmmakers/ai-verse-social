# Installation and execution

This package contains an executable Python engine and an agent skill. See [README.md](README.md) for the quick start. Python 3.11+, FFmpeg and an execution-capable host are required. Drive and local transcription libraries are optional modules. No account, customer video, brand asset, credential, private state, or paid model subscription is included.

## Install

Copy this complete folder into the host's supported skill directory. Keep `src/`, `scripts/`, and references together. The portable command works without installing the engine:

```text
python3.11 ABSOLUTE_SKILL_PATH/scripts/social.py --workspace ABSOLUTE_PRIVATE_WORKSPACE init
python3.11 ABSOLUTE_SKILL_PATH/scripts/social.py --workspace ABSOLUTE_PRIVATE_WORKSPACE audit
```

Alternatively install into a private virtual environment:

```text
python3.11 -m venv /your/private/runtime
/your/private/runtime/bin/python -m pip install '/installed/ai-verse-social-media[drive]'
```

For a local offline build, install the project build requirements first or use an available build toolchain with `python -m pip wheel --no-deps --no-build-isolation --wheel-dir DIST ./ai-verse-social-media`; then install the resulting wheel in the target Python 3.11+ environment. Normal customer installs should use a built release artifact from a trusted package source.

Use the matching Windows Python/virtual-environment paths on Windows. The `ai-verse-social` executable exposes the same commands. Do not install into another customer's runtime or overwrite another skill. Run onboarding after installation; settings alone do not establish authenticated access or start a scheduler.

Supply `ZERNIO_API_KEY` through the host's approved secret storage/environment. Never put it in the settings or messages. Google Drive can use an existing private authorized-user credential file (`AI_VERSE_GOOGLE_TOKEN`) or the bundled `drive-connect` flow with a customer-owned Google OAuth desktop client. A distributable OAuth application requires its own provider configuration and consent process; this package does not grant it. Only Drive access is requested, not unrelated Gmail/Calendar permissions.

For local transcript generation, install the `transcribe` extra (its model may download on first use), or provide `whisper-cli` plus a local model. A host's existing transcription tool can supply an actual transcript through `transcript-import`. Never require a transcript for a silent video; request context instead.

## Host behavior

- **Hermes:** Install the complete skill using its supported local-skill mechanism. Agent recurring jobs invoke this skill's operations reference, with an explicit workspace, timezone and standing authorization. A script tick can reconcile/submit already-prepared requests; it does not independently invent captions. Verify saved job definitions/read-back on the target Hermes installation.
- **Codex:** Install as a local skill. Use natural language or the skill invocation supported by that installation. Recurring execution uses that host's supported automation/scheduler; do not assume a copied skill creates one.
- **Claude Code:** Install as a project/personal skill according to the target host's supported structure. An optional `/ai-verse-social-media-onboard` command body is provided in `hosts/claude-code/commands/`; place it only when that host supports custom commands. It routes to the skill instead of duplicating business logic.
- **OpenClaw or another execution-capable agent:** Use its supported skill loader and map the portable command to its worker/scheduler. Verify attachment persistence, secrets, timezone, and process lifecycle before claiming unattended readiness.
- **Cloud-only chat:** Needs a configured persistent worker/tool integration to execute the engine and store state. It cannot be claimed supported solely from reading the Markdown.

Host-specific starter instructions are in [`hosts/hermes/README.md`](hosts/hermes/README.md), [`hosts/codex/README.md`](hosts/codex/README.md), [`hosts/claude-code/commands/`](hosts/claude-code/commands/), and [`hosts/openclaw/README.md`](hosts/openclaw/README.md). These document the required integration points; they are not certification of every host version.

All host setup is customer-specific and must be audited. The initial deployment owns one local workspace/database on one machine; don't put the live SQLite database in a synced/network folder or run independent writers on two hosts. A sleeping machine cannot dispatch local jobs; provider-side scheduled posts can still execute.

## Slash command

`/ai-verse-social-media-onboard` is the intended onboarding alias, with `/ai-verse-social-media-oboard` accepted as a spelling alias. Native command registration varies by host. The same flow is always available through 'set up my social media assistant' when the skill is loaded. Never advertise universal native slash-command registration without testing that host.

## Updates and uninstall

Pause new creates, run `backup /safe/private/location/customer.zip`, replace the installed package, audit, and reconcile provider schedules before resuming. `restore customer.zip --to /new/private/workspace` only restores into a new workspace and starts paused behind an engine-enforced quarantine. Reconnect host-managed secrets, verify the saved account inventory, reconcile provider posts/schedules created since the backup, then record that review with `restore-reconcile --note-file /private/review.txt`. A backup may predate already-published or scheduled posts. Never replace customer records with example files. Database versions fail closed when unsupported; migrations must be tested before an update. Uninstalling or pausing locally does not cancel remote scheduled posts. Inspect and intentionally cancel pending provider schedules if requested, then remove only this skill's jobs/services and installed files. Retain/export customer history according to their retention decision.

## Verification boundary

See `VERIFICATION.md` for local tests and outstanding release gates. This file describes installation, not a claim that all hosts/accounts have passed live tests. No public publishing or remote setup changes have been performed during development.

The `hosts/` directory contains starter guidance and onboarding aliases for Hermes and Claude Code. Hermes aliases are separate tiny skills; Claude command files are copyable into a supported `.claude/commands/` location (or ported to its current skill format). Codex and OpenClaw guidance explains the needed runtime/scheduler access. The slash surface and scheduler still need a smoke check against each customer's installed host version.
