# Engine interface

Run `python3.11 ABSOLUTE_SKILL/scripts/social.py --workspace ABSOLUTE_PRIVATE_WORKSPACE COMMAND`. JSON output is intended for agents. Files supplied to commands are private UTF-8/JSON artifacts, never shell-interpolated captions or credentials.

| Command | Purpose |
|---|---|
| `init`, `audit [--live]` | Preserve/create workspace and run bounded setup checks |
| `profiles`, `accounts [--sync]` | List profiles or refresh selected-profile account identities |
| `configure FILE` | Validate/save complete non-secret settings |
| `account-policy ID --enabled yes/no --capability FILE` | Explicit scope and verified capability contract |
| `drive-archive-policy --enabled yes/no` | Explicitly allow or stop copies of chat/local videos in the mapped Drive Posted folders |
| `ingest FILE [--library]`, `drive-ingest ID [--library]` | Preserve video and canonical history |
| `enroll ASSET --library yes/no` | Explicit future distribution policy |
| `drive-connect CLIENT TOKEN`, `drive-inventory FOLDER_KEY` | Optional OAuth and folder inventory |
| `drive-sync [--asset ID]` | Move fully verified Drive sources and, when explicitly enabled, upload chat/local videos to Posted/Library or Posted/Request-scoped; retry filing only |
| `drive-intake [--limit N] [--library]` | Page through the configured Ready folder, queue new files, ingest a small bounded batch |
| `edit ASSET [--recipe FILE]` | Source-preserving pass-through or standard rendering |
| `transcribe ASSET [--model-path FILE]`, `transcript-import ASSET FILE` | Actual local/host speech transcript |
| `caption-add FILE --kind KIND [--asset ID] [--destination ID] [--parent ID] [--note TEXT]` | Preserve user examples, proposals and revisions |
| `caption-context [--asset ID] [--destination ID]` | Retrieve current voice/preferences and examples |
| `next-times DESTINATION [--analytics]` | Per-account schedule recommendation with evidence/fallback |
| `prepare FILE` | Validate and freeze a platform-specific preview |
| `authorize REQUEST HASH --note-file FILE` | Bind actual authorization to the unchanged preview |
| `recover JOB reconcile/retry-failed/promote-draft --note-file FILE [--provider-id ID] [--request ID --payload-hash HASH]` | Reconcile proven existing attempts, retry an existing failed post once, or promote a reviewed draft without creating another post |
| `execute JOB`, `tick`, `cancel JOB` | Submit/reconcile with claims; cancel only unsent jobs or provider-confirmed scheduled posts |
| `status [--asset ID] [--limit N --offset N]`, `export` | Read paged per-account coverage and rebuild logs/journal |
| `pause [--resume]`, `backup FILE.zip`, `restore FILE.zip --to NEW_WORKSPACE`, `restore-reconcile --note-file FILE` | Operational control, full private workspace backup, paused restore, and post-restore reconciliation gate |

## Request specification

```json
{
  "asset_id": "canonical-video-id-returned-by-ingest",
  "accounts": ["exact-destination-id-returned-by-accounts"],
  "mode": "now",
  "captions": {
    "exact-destination-id-returned-by-accounts": {
      "text": "The customer's platform-specific caption.\nA real new line.",
      "platform_data": {}
    }
  }
}
```

`accounts` also accepts `all` or one unambiguous platform. Prefer exact destination IDs for multi-account work. `mode` is `now`, `schedule`, or `draft`. Scheduled requests require an offset-aware future `scheduled_at`, either at root or per-account caption entry. An edited engine rendition can be selected with `media`; original media is the default. `drive-intake` reads only one Drive listing page per run and downloads at most 10 files. It persists pagination and a change watermark; file revisions stop for review rather than being ingested as new videos.

Use current Zernio platform schemas to populate `platform_data`. It is passed as `platformSpecificData`, not invented generic fields. TikTok video posts require the account's allowed privacy level, `content_preview_confirmed`, `express_consent_given`, and comment/duet/stitch controls. YouTube can derive its title from the caption's first line; an explicitly supplied title is limited to 100 characters. Confirm all consent/privacy/disclosures and publication surface from the user's choices. Provider validation handles remaining account/media requirements; account health is checked separately. [Create post](https://docs.zernio.com/posts/create-post), [TikTok requirements](https://docs.zernio.com/platforms/tiktok), [YouTube requirements](https://docs.zernio.com/platforms/youtube), [Validate post](https://docs.zernio.com/validate/validate-post).

The bounded live audit uses Zernio's profile-filtered bulk health endpoint and requires `status: healthy` plus `canPost: true`; each actual publish also checks that exact account's detailed health immediately before upload. See [bulk account health](https://docs.zernio.com/accounts/get-all-accounts-health) and [single-account health](https://docs.zernio.com/accounts/get-account-health).

`backup` stores settings, the consistent database, logs, captions, request records, transcripts, and media; secrets are excluded and must be reconnected from the host's secret store. Store the archive outside the live workspace. `restore FILE.zip --to NEW_WORKSPACE` is the only command that does not require `--workspace`. It accepts only a supported, checksummed archive and a new workspace path. It starts paused and in a database-enforced quarantine; reconnect secrets, confirm saved accounts still match Zernio, and manually reconcile posts/schedules created since the backup. Then `restore-reconcile --note-file FILE` records the review and clears quarantine. Explicitly resume after that; posts already scheduled with a provider may execute even while the local workspace is paused.

`drive-archive-policy --enabled yes/no` controls whether verified local/chat uploads are copied to Drive. It is off by default so connecting Drive alone does not silently upload content. When enabled, library videos go to the mapped `posted` folder and request-only videos go to `request_scoped`. A stable Google Drive file ID is saved before transfer, so a restart or lost response reconciles the same file instead of creating another copy. Drive-origin videos are moved, not re-uploaded.

The export reserves its ID with Drive `files.generateIds` and uses that ID for file creation. Google documents that a successful create followed by a retry with the same generated ID returns a conflict instead of creating a duplicate. This behavior is covered with a fake-service response-loss test; the provider contract still needs real-account verification. [Google Drive create-file guide](https://developers.google.com/workspace/drive/api/guides/create-file).

`cancel JOB` marks unsent work cancelled locally. For a provider record, it reads the post first and deletes only a still-scheduled record. Zernio DELETE removes the record: confirmation can be a cancelled record or authenticated GET 404 after the persisted cancellation intent. A lost response reconciles that same intent. Publication is blocked while cancellation is pending. A worker already sending may still publish during the cancellation race; deleting its provider record cannot undo a platform post. Never report this as guaranteed unpublishing. [Official delete contract](https://docs.zernio.com/posts/delete-post).

Recovery uses the existing provider record. `recover JOB reconcile --provider-id ID --note-file FILE` links an unknown outcome only if returned job/asset metadata and destination match the saved intent. `retry-failed` requires a confirmed failed record, immutable authorization and healthy destination; its durable intent prevents resending an uncertain retry. `promote-draft` requires a new `prepare` preview/hash and a note, preserves the same account/media, and updates the draft with explicit publication timing. It does not first call `authorize`, which would reuse the existing cycle. After an uncertain recovery, reconcile; don't keep triggering retry calls. If investigation cannot prove the outcome, report blocked for manual provider review. [Retry](https://docs.zernio.com/posts/retry-post), [Update](https://docs.zernio.com/posts/update-post).

Cancelled cycles can be replaced only by a newly prepared and authorized request; prior job evidence is retained in events. Intentional reposting of a verified video to the same account requires a separately designed campaign/repost policy and is excluded from default automation. Adding a new account does not count as reposting an existing account.

`media_by_account` maps exact destination IDs to different validated engine renditions. Each output must have QC tying its output hash to this canonical source hash; requests freeze each account's media hash. The root `media` remains the default. Logs expose final platform identifiers/URLs; status also distinguishes request coverage and pending storage exports. CSV/JSON exports never authorize a post.

Backup restores relocate canonical media, rendition, branding and local action paths, rebind request hashes only for relocation, and keep submitted provider payloads unchanged. Original media and pre-relocation authorization hashes are validated. Restored work remains quarantined until account/history review.

Drive sync processes rotating bounded batches (not the whole library) and `--asset` filters both moves and local copies. It refuses changed source versions and requires content checksum proof for legacy sources without version records. Rerun storage synchronization to drain backlog; never rerun publication to repair storage.


A capability object records `video: true`, an `evidence` source, optional `caption_limit`, `max_bytes`, `max_duration`, `required_fields`, and `proof` (`public_url` default, `provider_identifier` for an explicitly configured non-public proof route). Capability configuration is not a live successful-post claim. The onboarding audit must establish the actual route.
