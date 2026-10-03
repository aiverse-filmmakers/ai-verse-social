# What this package does today

This matrix describes the bundled code as it is, not every feature an agent host or social platform might support.

| Area | Included behavior | Readiness boundary |
|---|---|---|
| Live video request | An agent can use an accessible local attachment or a configured Drive video, prepare platform-specific captions, and send the request through the same engine as recurring work. | The host must expose the actual file to the engine. Host attachment persistence has not been certified on each named host. |
| Connected social accounts | Refresh accounts in selected Zernio profiles; keep separate account histories, handle reconnection, and make future-account backfill a customer setting. | Video capability is set per account. A new connected platform is not automatically certified just because Zernio lists it. |
| Publish, schedule, draft | Validate the media and provider payload, check account posting health, persist authorization and idempotency state, submit, then reconcile provider status and platform evidence. | No real account or public post has been tested. Provider capability, consent fields and proof may vary by platform/account. |
| Per-account history | Durable SQLite records plus rebuildable CSV/JSON logs; partial success stays per destination. Verified Drive originals move only after coverage closes. | Logs are local workspace data; host backup and retention remain the customer's responsibility. |
| Google Drive intake | OAuth connection, bounded folder inventory, resumable intake, source-version checks, safe move recovery. | Requires a customer OAuth client and Drive authorization; no live customer Drive test yet. |
| Chat/local video archive | Optional copy into mapped library or request-only Drive folders after requested destinations verify. A pre-reserved Drive ID makes retries reconcile the same file. | Off by default; customer enables `drive-archive-policy`. Tested against a fake Drive service, not a real account. |
| Repeated footage | Local three-frame comparison, review holds, nine unique-work thumbnails, confirmed work/account coverage and reversible extra-upload filing. Shared by live/scheduled creates. | Sampled heuristic, not full video/audio identity; missing history/tools hold new candidates. Optional vision review and Drive mapping require setup. Real clips and simulated failures tested locally. |
| Editing | Preserve-source passthrough and deterministic FFmpeg recipes for trim, dimensions/padding, watermark, subtitles and audio normalization. | Does not generate creative edits. FFmpeg and enabled recipe assets must be installed and checked on the host. |
| Captions | Agent-written platform variants, Unicode-safe storage/round trips, transcripts when supplied/generated, and a private example/correction journal. | The engine does not itself contain a model or guarantee platform performance. |
| Recurring operation | Bounded intake and publish/reconciliation ticks, timezone-aware windows, spacing, daily caps, pause, and explicit cancellation/recovery. | A skill install does not create a scheduler. The target host needs one persistent worker and one schedule owner. |
| Recovery | Full private-workspace backup; restore into a new paused, quarantined workspace; account/history reconciliation before resume. | Real post-backup provider history must be reviewed by the customer after restore. |
| Comments and DMs setup | Optional guided questions, resumable requirements and verification of an available Zernio/host engagement route. | The local video engine has no engagement worker. Sending/automation requires explicit scope, a working integration, durable response tracking and account-specific tests. |
| Skool scraping | Not part of this package's core publishing skill. | An optional intake module would need its own permissions, data handling and tests. |
| Host commands | Skill guidance plus starter host notes and onboarding aliases for Hermes and Claude Code; Codex/OpenClaw integration guidance. | Slash registration, attachment handoff, scheduler persistence and secrets are not certified on actual host installations. |

For test evidence and remaining release gates, see [VERIFICATION.md](VERIFICATION.md). For source reuse and rights status, see [REUSE.md](REUSE.md).
