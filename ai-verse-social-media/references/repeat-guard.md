# Repeated-footage protection

Use this for repeat holds, review, thumbnail evidence and duplicate-source filing. Live and scheduled publishing use the same engine guard; never add lane-specific bypasses.

## What it does

Exact file bytes reuse the existing asset. Different encodings get three original-source samples (20%, 50%, 80%) and local DCT perceptual hashes. Two informative corresponding matches within the configured threshold, with similar duration, **request review**; they do not prove identity. One similar shot alone does not block a normal clip. Black/static or insufficient evidence needs review.

The visual comparison includes known verified work from the past 60 days and unresolved/prepared work in the selected profiles. Exact bytes and confirmed work/account history persist beyond that window. Published history requires verified platform receipts. Coverage is per connected account, so a new account on an existing platform can receive eligible work too.

## Agent workflow

1. Read `status` coverage and `eligible_accounts`; automatic selection requires library enrollment and no hold. Call `repeat-check ASSET --accounts SELECTOR` before treating it as a candidate. `prepare`/`authorize`/first-create execution enforce the guard again.
2. `clear`: proceed under normal publishing permissions. `already_covered_or_reserved`: return the genuine prior job/receipt; do not create a replacement. Mixed requests prepare only eligible accounts.
3. `check_pending`: explain the missing check and resume `repeat-index --limit 3` in separate bounded runs. Do not spin indefinitely. Missing tools/media need repair. Existing-workspace `audit` reports cache gaps without decoding or publishing.
4. `needs_review`: keep the source in Ready, excluded from automatic selection. Generate `repeat-panel --asset ASSET`; it shows the last nine unique published works, candidate samples and three-frame evidence for matches even outside the grid. Pending tiles are explicitly labeled. Read the legend and show relevant evidence. Agent visual inspection is optional and may use the host's vision capability; disclose that when used.
5. Record the customer's or explicitly authorized review: `repeat-review ASSET --decision same|distinct --match WORK --note-file FILE`. “Same” links work identities only when active/uncertain jobs have been reconciled/cancelled. “Distinct” clears only the compared pair under the current hashes/policy; use no match only to clear low-information footage after review. Similar styles/themes are insufficient for “same.”
6. Run `repeat-check` again; it resumes cleared unsent `repeat_hold` jobs. Existing submitted/provider attempts reconcile with their actual records even while new candidates are held. Never reset unknown outcomes to fresh creates.

## Folder filing

Map an approved `drive.folders.repeat_review` folder, normally named “Repeat Material - Review.” `repeat-sync` files only exact/confirmed **extra uploads**, after outstanding authorized delivery is complete. Canonical published sources stay untouched. Uncertain matches remain Ready. Missing Drive or folder mapping leaves local blocking functional.

`repeat-restore SOURCE_ID` returns a filed extra to its recorded original Ready folder. Pending move/restore actions reconcile via metadata/checksum read-back; rerun `repeat-sync`, never publication. Source revision, checksum or folder-scope changes stop filing for review. Restore preserves receipts and cannot authorize a repeat post. A restored extra remains excluded from ordinary archive filing; its work history still controls eligibility.

## Settings and limits

Defaults in `repeat_guard`: enabled, `window_days: 60`, `hamming_bits: 6`, `duration_tolerance: 0.05`, `index_batch: 3`, `batch_seconds: 120`. Hash algorithm `dct32-low8-ac63-v1`: 32×32 grayscale DCT, 63 AC bits in a 64-bit container. Thresholds are starting heuristics, not universal guarantees. Indexing and panels checkpoint rather than decoding the whole library. Cached source/frame checksums, source stat, algorithm and pair policy invalidate stale evidence.

Use `repeat-policy --enabled no|yes --note-file FILE` to record an explicit customer choice. Disabling visual checks preserves exact bytes and confirmed work/account protection. Never disable it silently to make a held job succeed.

This samples footage; it can miss trims, crops, reordered scenes, brief repeats or re-encoded copies outside the window. Same pictures may have different speech, music or meaning; review decides that distinction. It is not full video QC, audio similarity or a mandatory vision-model pass. Ordinary engine media validation remains active.

## Upgrade/recovery

Version 0.2.0 atomically migrates database schema 1→2, preserving jobs/receipts. Make a private workspace backup before upgrade. Old schema-1 engines must not open migrated state; restore an old backup into a separate workspace if rollback is necessary. New backups include decisions, claims, filing state and cached frames. Restore verifies/rebinds files and starts paused/quarantined; complete normal account/history reconciliation before resuming.
