# Source reuse and changes

The original capture remains unchanged. This release adapts the operating workflow and reviewed helpers; it does not bundle customer IDs, state, branding, source media, or private placeholder implementations.

| Captured component | Use in the new package |
|---|---|
| `source/project/scripts/caption_integrity.py` | Copied into the engine; exact JSON caption round-trip helpers retained. International-format character handling is reviewed separately rather than imposing personal caption rules. |
| `source/project/scripts/social_drive_ops.py` | Bounded transient-error retry helper extracted into the Drive adapter. Account claims and completion replace the unsafe JSON ledger/key-presence checks. |
| `source/project/scripts/zernio_publish_manifest.py` | Its checkpoint-before-poll and exact-caption verification workflow retained. Account destinations are dynamic; provider credentials and APIs are configurable; current documented Idempotency-Key support is used. |
| Captured media rendering scripts | Their source-preserving, cached-render, probe/decode and upload-verification workflow retained. Customer editing recipes replace AI-Verse assets and machine-specific paths. |
| Captured recurring prompts/skills | Reference for the shared live/scheduled workflow. Personal cadence, platform lists, branding and source rules are replaced by customer configuration. |

New code is limited to the portable execution boundary, transactional account state, onboarding, caption memory, shared live intent and integration needed by the new requirements. The original scripts are not runnable unchanged: many depend on withheld resources and an installed Hermes profile.

The package owner confirmed on 2026-10-03 that they own the reused first-party source and can grant commercial rights. Customer-facing license terms have not yet been selected, so this package intentionally ships without a license grant. Third-party libraries are installed as dependencies, not copied into the release; their use remains subject to the applicable upstream licenses and provider terms.
