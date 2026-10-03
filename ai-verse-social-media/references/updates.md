# Weekly update checks

Offer this during setup: “Would you like a silent weekly check for new versions?” A check only reads the repository; it never replaces skill files or changes posting jobs. The customer enables it once. Do not imply that installing the skill activates a scheduler.

Use exactly one host-native recurring job per private workspace. Respect the host's scheduler and permission controls. Reuse an existing matching job instead of adding duplicates. Default to weekly Monday at 10:00 in the customer's configured IANA timezone; record any preference. Read back the saved job and next run before claiming it works. If the host cannot suppress unchanged notifications, use a scheduler/worker that can or report the limitation; do not promise silence.

The scheduled agent should:

1. Read this installed skill's `LOCAL-SETUP.json` to resolve its Python, skill path and workspace. Run `scripts/check_updates.py --state ABSOLUTE_PRIVATE_WORKSPACE/update-check.json` with that Python. This subprocess has a 30-second network timeout. Do not audit accounts, scan content or contact social platforms.
2. Empty output means no new notification: finish silently. Do not announce a successful check. The checker saves private status and suppresses repeat notifications for the same available revision or unresolved problem.
3. `update_available` means the main branch differs from the installed revision. Tell the customer once that a new revision is available and they can ask to update. A revision change does not prove that it is a newer compatible release; never install it automatically.
4. `needs_attention` means authentication, internet, Git or installed revision needs attention. Explain once and save the blocker. Do not retry indefinitely or claim no update exists.

Private repositories require an authenticated Git credential helper usable by `git ls-remote` in the scheduled worker. Signing in only to an agent's GitHub connector may not authenticate Git. Do not put tokens in prompts, URLs, job definitions or logs. Verify worker access before enabling. Machines must be awake and the chosen scheduler running; missed-run behavior follows the host.

For Hermes, use its current supported cron API; for Codex use its automation tools; for OpenClaw use its scheduler; for Claude Code use a supported persistent external scheduler when native recurring execution is unavailable. Do not invent a universal cron command. Save job identity, timezone and status in private `update-monitor.json`. Cancellation removes only this update checker, never publishing jobs.

When updating: follow INSTALL.md, back up private data, pause relevant publishing workers, use the repo installer with `--update` and explicit host/directory/workspace arguments, then audit and reconcile before resuming. Re-read the saved weekly job so it still resolves the installed skill. Personal edits stop the installer. Repository access alone is not automatic-update authorization.
