# Current verification and remaining release gates

This document records development evidence, not a claim of universal host/platform certification.

**Release status:** technical release candidate for controlled setup; not yet ready for public sale. The package owner confirmed rights to reuse the source on 2026-10-03. Purchaser license terms still need to be selected; direct optional-dependency notices are included and exact installed/runtime versions still need review; the package ships without a customer license grant.

## Verified locally

- Python 3.11 engine executes from a copied skill folder without installing a runtime package.
- 96 behavioral checks (including one real FFmpeg media test) cover account addition/backfill, reconnection identity, multiple accounts on one platform, publication proof gates, duplicate request reuse, create recovery after restart and simulated database failures before/after provider acceptance without duplicate posts, cancellation of local/provider-scheduled work and protection of published/unknown outcomes, claim exclusion, caption revisions/learning, scoped versus library archives, approved schedule windows/limits, multilingual caption preservation, late provider reconciliation, bounded Drive intake pagination and move recovery including folder-map changes during source moves and local archive uploads, stable Drive ID retry after a lost upload response and engine restart, explicit policy opt-in, safe additive schema upgrade, TikTok consent gates and corrected definitive rejections, batched read-only live account-health/posting-permission audit with allocated timeouts and no Drive token refresh, offline audit readiness gating, full-workspace backup/paused restore/checksum and traversal rejection, offline backup CLI, restore quarantine/identity-match requirements, restore CLI behavior, plus the Zernio adapter's exact UTF-8 payload/idempotency behavior.
- A focused Google Drive transport check confirms the live audit does not refresh expired tokens or retry after an unauthorized response.
- `skill-creator` metadata validator passes; first-run live audit returns a readable missing-setup checklist with no secrets configured.
- A Python 3.11 wheel builds locally, installs into a fresh virtual environment, and its installed CLI initializes, audits, backs up, and restores a private workspace into quarantine.

## Still required before public release

- Actual connected-account contract tests for Google Drive and Zernio, including scheduled/private or approved public publication, remote folder transitions, account changes, cancellation/retry and provider proof variations.
- Host installation/attachment/scheduler smoke tests on each advertised host and confirmation that the shipped Hermes/Claude onboarding aliases show up in the installed command palette.
- Review of supported platform capability contracts and customer onboarding routes; verify TikTok consent, YouTube privacy/title and other account-specific requirements.
- Live fault tests for database/disk failures, restart during upload, restore against real post-backup provider history, and unknown-state reconciliation. Simulated checks cover database failures before/after provider acceptance, Drive upload response loss with a reserved ID, and folder-map changes during remote moves/uploads.
- Customer-authorized Google Drive OAuth/intake/move tests and Zernio account/platform tests; only fake-service tests have been run here.
- Verification that each host's persistent worker can access the same workspace, secret store, FFmpeg and media attachments.
- Customer-facing license terms and third-party dependency notices for selected optional install extras.

No test that mocks a provider proves that the provider or a real customer account is connected. No public posts/messages or remote writes have been performed during this build.

## Independent audit completed 2026-10-03

The audit did not rely on the earlier handoff as correctness evidence. It fixed restoration path rebinding, atomic receipts/obligations, active-lease revisions, mutable-job/cached-body validation, per-account media provenance, actual DELETE/404 cancellation semantics, paused reconciliation, queue fairness/archive retries, Drive revision/scope/filter/batch checks, native account identity/scope validation, strict settings and response handling, known-record recovery/draft promotion, cache correctness/streaming, final-proof logs, caption context and private-network upload rejection.

The suite contains the original 64 checks plus 32 independent audit regressions, including source/output QC, a real MOV pass-through cache and an actual padded FFmpeg render. Tests simulate provider faults; they do not represent live publishing. The complete finding/repair history lives in the owner's root AUDIT-2026-10-03.md; customer releases contain this concise evidence summary.

Run offline checks with Python 3.11+: `PYTHONPATH=src python3.11 -m unittest discover -s tests -q` from the package directory. See RELEASE-CHECKLIST.md for the fixed live acceptance finish line.
