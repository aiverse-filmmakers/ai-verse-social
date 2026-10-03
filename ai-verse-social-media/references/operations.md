# Live requests and recurring operations

Natural language is the interface. Examples illustrate intent; never require an exact sentence. Clarify only material ambiguity about the video, customer/account, timing, or required editing.

## Live request

1. Persist a host attachment or download a configured Drive file through `drive-ingest`. Ingest gives the canonical asset ID and flags duplicates. If attachment access expires, archive before promising a future post. Never use an inaccessible metadata-only attachment as a real video.
2. Determine `now`, `schedule`, or `draft`, selected accounts, and edits. 'All' resolves to included accounts within the configured profile scope; a platform with multiple accounts needs explicit destination IDs or a saved default. A live 'only' request restricts scope.
3. Read caption context; save the customer's idea/custom input/correction. Transcribe only when useful/required, using an existing host tool or bundled local transcription. Inspect visuals for context. Apply the configured editing recipe if requested; reuse its cached output. When accounts require different formats/lengths, render separate variants and bind them through `media_by_account`; QC must match this source and each output.
4. Obtain per-account next-time recommendations when requested. They respect customer windows, spacing and reservations, using historical evidence when available; report fallback honestly. A best time is not a guaranteed optimum.
5. Run the [repeat guard](repeat-guard.md); resolve `check_pending` or `needs_review` before new sending. A covered-work result is a no-op with the original receipt. Write a JSON specification using the engine interface. `prepare` produces an immutable preview and payload hash. When authorized by the user's request or standing scope (and preview policy is satisfied), `authorize` with the same hash and private note evidence.
6. `execute` each job or `tick`. It stores provider IDs before verification, reuses existing attempts, and requires evidence before completion. Return accepted schedules immediately; verification continues in later bounded ticks.
7. `status` shows partial outcomes; `export` refreshes customer-readable logs/journal. When the configured library coverage is complete, `drive-sync` moves its Drive original to Posted. For chat/local videos, it copies the verified original only when the customer enabled `drive-archive-policy`; the copy uses a reserved Drive ID and retries the same file after uncertain results. A filing error is retried as filing only. Do not claim a failed upload/move is done or retry an already-published post to repair storage.

Live input defaults to request-only distribution. Enroll in the recurring library only under the user's configured preference or explicit request. A scoped request's completion is different from coverage of every connected account; its local archive is labeled `posted/request-scoped`, while library coverage is `posted/library`. Drive filing uses the matching configured Posted folder, including `request_scoped` for the scoped archive. Explain 'Instagram complete; not enrolled elsewhere' where appropriate.

## Recurring gather / edit / select / post

Configure separate bounded jobs: `drive-intake`, processing, account refresh, agent drafting/selection, `tick` submission/reconciliation, and optional analytics. `drive-intake` scans one Drive page and downloads no more than its configured batch limit; `tick` submits/reconciles already-prepared authorized manifests and never invents captions. Agent drafting jobs use this skill and the customer voice context.

For inventory, list configured source folders, including Posted only for authorized library backfill. Re-ingestion returns the canonical asset so uploading/renaming/moving cannot reset its history. Ingest a bounded number of unseen ready videos; preserve incoming originals. Do not pull draft/review/generated outputs as new raw candidates.

Select pending obligations per destination from paged `status` coverage, not only the Ready folder. An already-Posted library asset may be the correct candidate for a new account. Use `eligible_accounts` and exclude `repeat_hold`, existing work/account jobs and confirmed coverage. Call `repeat-check` before final selection; resume bounded indexing/review separately. Respect freshness/rights, eligibility, approvals, normal cadence and backlog limits. Account-wide outages pause only that lane; a failed rendition doesn't reject all videos from its author.

Use customer templates and actual video context to draft a new request; bind current standing authorization. Scheduled and live jobs share claims, records, validators and recovery. No repeated full-library media decoding or unbounded candidate searches. Save a checkpoint and continue next tick when the item/time budget ends.

One host worker owns the workspace. Use provider scheduling for future ready posts or an awake local worker for dispatch, not two simultaneous owners for a slot. A chat request to publish an already-scheduled video must reconcile/change that schedule; it must not silently create a second post. Cancellation and failed-post retry are explicit recovery actions.

When the user explicitly asks to cancel a scheduled post, resolve its exact job ID and run `cancel JOB`. The engine checks the provider's current status, cancels only a still-scheduled post, and confirms a cancelled result or authenticated absence after the saved deletion intent afterward. If it already published or the create result is unknown, explain that state and do not issue a delete.
