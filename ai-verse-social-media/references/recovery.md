# Account changes, partial results and recovery

- Refresh complete profile-scoped account listings. Partial discovery cannot remove accounts. Temporary disconnection keeps obligations/history; explicit disable retires current scope without manufacturing successful publications.
- Reconnect by validated underlying account identity. If a provider ID changed and no native identifier proves the mapping, ask to confirm the relationship; don't guess based on display name.
- New authorized destinations get their own logs and pending obligations for library assets, including Posted videos. Request-only assets remain restricted until enrolled. Unknown video capability blocks posting with a clear setup task.
- A job's provider ID means fetch/reconcile that record. Successful destinations are never recreated; missing IDs/outcome uncertainty require the original still-valid Idempotency-Key or manual evidence reconciliation.
- Zernio documents a 24-hour Idempotency-Key window. The engine uses a conservative 23-hour retry cutoff. Past that window, unknown outcomes cannot safely be retried automatically. A correlation header alone is not sufficient. [Create post](https://docs.zernio.com/posts/create-post).
- A scheduled job is not verified publication. A failed comment/Sheet/Drive operation does not undo a published video. Retry the specific remaining operation.
- Drive filing rechecks the configured destination immediately before and after each remote move. If the mapping changes during that move, preserve the observed parent and retry only the move toward the new configured folder.
- The database is authoritative. CSV, Markdown journal and optional Sheets are derived views; rebuilding them doesn't authorize reposting. Do not edit/delete job rows to force a retry.
- After backup restore, the engine blocks authorization and execution even if a user accidentally clears the visible pause. Reconnect secrets, refresh/verify saved provider account identities, inspect posts and pending schedules created after the backup, then record the review with `restore-reconcile --note-file FILE`; only then explicitly resume. Restore never overwrites an existing workspace, and a stale backup can miss live posts created after its timestamp.
- Pausing locally does not cancel provider-side schedules. Show pending provider identifiers and ask/apply explicit cancellation scope rather than claiming all activity stopped.
- For an explicit cancellation request, use `cancel JOB`; it never deletes a published post and refuses an attempt whose create result is unknown. If the provider status is already published, reconcile the receipt instead of cancelling.

Always preserve the original and attempt evidence. Report account, stage, what is known, what is unknown, and one next action. Do not convert missing proof into failed/successful status just to clear a queue.

## Explicit recovery commands

Use `recover JOB reconcile --note-file FILE` for a known post. For an unknown create outcome, add `--provider-id ID` only after finding the saved job/asset metadata on that provider record. Names and matching captions alone do not prove ownership.

Use `recover JOB retry-failed --note-file FILE` to retry a confirmed failed provider record, preserving its identity. An uncertain retry is reconciled only; repeated chat messages cannot resend it automatically. For a provider draft, prepare a new publication preview and use `recover JOB promote-draft --request REQUEST --payload-hash HASH --note-file FILE`. Keep the same account/media; editing requires a separate reviewed operation. See [engine interface](engine.md).

Local pause blocks sending but permits fetching existing provider receipts. Disabled/disconnected accounts still allow reconciliation of the original saved provider ID; changed profile/native identities cannot authorize a new send.

Cancellation DELETE removes the provider record, so a durable cancellation intent followed by authenticated absence is valid cancellation evidence. It cannot prove a platform worker did not publish during the race. Never promise that local pause or provider deletion unpublishes content. Explicit cancelled cycles require a fresh reviewed request before reuse; previous evidence stays in events.

Storage failures are separate from publication failures. `tick` retries pending local archive actions, `export` rebuilds logs, and bounded `drive-sync` repairs cloud filing. Look at `derived_export_error`, `sync_actions` and `drive_exports` in status; retain receipts even when storage is unavailable.

For `repeat_hold`, follow [repeat protection](repeat-guard.md): index or record review, then `repeat-check` to resume unsent jobs. Continue existing provider reconciliation. Never clear receipts/unknown claims to repair a hold. Retry duplicate-source filing/restoration through `repeat-sync` rather than sending again.
