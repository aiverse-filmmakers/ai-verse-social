# Weekly update checker audit — 2026-10-03

Scope: scripts/check_updates.py, installer revision metadata, weekly job guidance and their tests only. No publishing engine changes or real user scheduler activation.

Findings recorded BEFORE implementation:

1. HIGH — Notification is marked as seen before the scheduler delivers it. A failed/interrupted delivery loses the only notification. Fix: durable pending event; explicit acknowledgement only after successful user delivery; keep retrying pending delivery next bounded run.
2. MEDIUM — One notification key conflates update state and connection failures. Update A → temporary network failure → update A repeats the update announcement. Fix: separate acknowledged event IDs and pending outbox; deduplicate each revision/error independently.
3. HIGH — Malformed JSON/non-object setup/state, unwritable state and timeout paths can emit an uncontrolled traceback instead of one understandable blocker. Fix: strict bounded validation, sanitised errors, atomic state writes, stable exit behavior; preserve corrupt state rather than pretending up-to-date.
4. MEDIUM — Concurrent checks can read the same old state and create duplicate events. Fix: nonblocking per-state file lock around read/check/save/ack; busy run finishes silently; lock releases after crash.
5. MEDIUM — Private install via authenticated `gh` can succeed while HTTPS Git has no credential helper; the checker insists on Git. Fix: prefer authenticated GitHub CLI API when available, fall back to noninteractive Git for public/existing Git-helper auth; never expose credentials/provider stderr. Probe exact worker environment in setup.
6. MEDIUM — Installer calls every checked-out HEAD the installed revision even if source files are locally modified. Fix: only record official repository clean checkout revision; modified/offline/unverified installs remain explicitly unverified rather than falsely current. Same-files/different commit remains a changed repository revision, not a certified release.
7. MEDIUM — Host schedule/quiet notification routing has no tested universal adapter. Fix: explicit native scheduler setup with saved job identity, conditional delivery and acknowledgement, read-back and silent-run test. Keep unsupported hosts in pending state; do not claim a running cron merely from copied docs.
8. LOW — Prior tests cover only three happy/error cases. Fix: meaningful regressions for pending retry/ack, mixed failures, corrupt state/setup, private gh route, timeouts, locking, unknown revision, and real local git installer metadata.

Preserve requirements: one weekly job per customer workspace, silent unchanged run, check-only (no downloads/installation/publishing), bounded network calls, original source capture excluded. New setup options do not activate any existing user's scheduler without their request.

Implementation and validation evidence: pending.

9. LOW — Weekly agent sessions are unnecessary when no event exists. Official Hermes docs support a pre-run `wakeAgent` gate. Fix: add optional `--hermes-gate`, using the same checker/outbox; no-change output suppresses inference and delivery. Use only where verified, not as a universal host marker. Direct script-only delivery must not acknowledge before the scheduler actually delivers.

## Fix disposition and validation

All nine findings addressed in the checker, revision metadata, host setup guidance and regression tests. This changes only update monitoring and its install metadata; the publication engine is unchanged.

Implemented: atomic locked status, bounded gh/Git fallback, strict metadata validation, corrupt-state preservation, sanitized storage failures, pending notification outbox, confirmation-only acknowledgement, independent event deduplication, clean official Git revision evidence, and Hermes pre-run inference gate. Network calls share one total 30-second budget. Older observed-only status migrates without pretending a notification was delivered.

Verified offline: 24 update-check tests (including actual CLI silence/gate/ack), 9 installer tests (including actual local Git checkout metadata), all 96 existing engine tests, and main skill validation. Total: 129 automated tests passed.

Host delivery remains customer-specific: no real customer cron, Hermes gateway notification, private GitHub account or social account was activated in this audit. Synthetic/private CLI routing was tested; real scheduler silence and receipt support are explicit setup gates. If unavailable, the monitor stays pending. Windows lock branch is implemented but not exercised on this macOS host.

Limits documented: exactly-once external messaging cannot be guaranteed across a crash after send/before ack; old acknowledged IDs are retained for the latest 128 events; storage failures cannot be durably deduplicated until storage is repaired; changed main-branch commits are changes, not certified compatible releases. No auto-install was introduced.

Official references checked: Hermes scheduled tasks (wakeAgent gate, SILENT suppression, script roots, credential passthrough, delivery evidence), GitHub CLI API authentication and Git credential behavior. These are setup instructions, not invented universal cron commands.
