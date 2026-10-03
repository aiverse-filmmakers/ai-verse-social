# Weekly update checks

Offer a silent weekly check during setup. The customer enables it once and chooses where notices go (their chat, never an unrelated recipient). Installing files does not activate a scheduler. This only checks repository revisions; it never installs code, audits social accounts or changes publishing jobs.

## Configure one job

Use one native job per private workspace. Locate an existing matching job by saved identity and workspace before creating anything; edit/reuse it. Default to Monday at 10:00 in the customer's IANA timezone, or their requested day/time. Save the actual job ID, timezone, skill path, state path, notification destination, enabled status, and delivery method in private `update-monitor.json`. Read back its next run. Keep missing scheduler/authentication/delivery controls as pending setup.

Read `LOCAL-SETUP.json` for the installed Python, skill and private workspace paths. The checker requires the installer's verified clean repository revision; ZIP/manual/modified installs without it cannot claim up-to-date status. Use the official clean checkout installer to enable this feature; do not write a guessed revision into installed metadata.

The worker runs:

```text
INSTALLED_PYTHON INSTALLED_SKILL/scripts/check_updates.py --state PRIVATE_WORKSPACE/update-check.json
```

Resolve these labels to actual absolute paths when saving the job. The script uses at most 30 seconds across GitHub CLI and Git network attempts. It saves private state under an OS lock and releases the lock after a crash. An overlapping run skips silently.

## Silence and reliable notifications

- Exit 0 with empty output: no pending notice; suppress delivery using the host's actual quiet mechanism. Never send “checked successfully” or “no update”.
- Each JSON output line is a pending notice with `event_id`. A changed revision produces `update_available`; an access/setup problem produces `needs_attention`. Summarise the outstanding notices in one short message. Do not send technical IDs to the customer or claim that a changed commit is a certified compatible release.
- Notices remain pending until **confirmed user delivery**, so an interrupted send is retried next week. After a supported messaging tool or scheduler returns positive delivery evidence, acknowledge each delivered event:

```text
INSTALLED_PYTHON INSTALLED_SKILL/scripts/check_updates.py --state PRIVATE_WORKSPACE/update-check.json --ack EVENT_ID --delivery-receipt NONSECRET_MESSAGE_ID
```

Repeat `--ack` for multiple events covered by the same confirmed message. This command performs no network check. Retain the host delivery reference; never supply a token or credential as a receipt.

Do not acknowledge when a final response is merely composed/queued. If the host only delivers the final response after the run ends, save the emitted event IDs and actual run identity privately; on the next run use supported delivery history/receipts to confirm that exact prior notification before acknowledging. A failed, queued or unverified result stays pending. If the host exposes no delivery evidence, leave monitor setup pending and explain the missing integration. Do not trade missed notifications for apparent silence.

For direct tool delivery, finish with the host's silent marker after sending/acknowledging; otherwise its automatic final delivery could send a duplicate. A crash after sending but before saving acknowledgement may repeat a notice: this is deliberate recoverable delivery, not a promise of impossible exactly-once messaging.

Acknowledged revision notices survive temporary network failures; they are not re-announced after recovery. A genuinely new outage after a healthy check can alert again. Damaged private status is preserved in a `.corrupt-…` file and reported; acknowledgement history may need review. Storage/unsupported-format errors exit 2 with a sanitised blocker rather than a traceback: repair/pause the job, since a checker without writable durable state cannot guarantee repeat suppression. Retained notice IDs are bounded to the latest 128; older events may be reported again after enough changes.

## Efficient host routing

**Hermes:** Prefer its pre-run script gate on versions supporting `script` and `wakeAgent`. Create a small wrapper under the active `$HERMES_HOME/scripts/` that invokes the installed checker with `--hermes-gate`; pin/use the installed Python 3.11+. The checker emits `wakeAgent: false` when nothing is pending, avoiding an LLM turn and delivery. When true, the agent handles pending notices with the confirmed-delivery/acknowledgement flow above. Use `cronjob_manage` and verify the saved script, timezone and delivery route. `[SILENT]` suppresses successful agent final delivery. Do not use script-only stdout delivery unless its adapter confirms delivery before acknowledging. The scheduler sanitises script secrets: verify that the correct profile's approved credential passthrough or credential helper actually reaches the worker. Never change global notification settings to silence just this job. See [Hermes cron documentation](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron/).

**Other hosts:** Use the available scheduler/automation API and verified conditional delivery. Add a pre-run script gate when supported; otherwise one bounded weekly agent run is acceptable. Do not assume Hermes markers or native persistent cron exist in every agent. If delivery acknowledgement is unsupported, leave the monitor pending. Never create a second checker to compensate.

## Setup tests and private repositories

Prefer GitHub CLI authentication when available; otherwise use noninteractive HTTPS Git credentials. An agent's GitHub connector alone may not authenticate either CLI. Public repositories can use anonymous Git fallback. Tokens must stay in approved secret storage/credential helpers, never prompts, URLs or job files. See [GitHub CLI API](https://cli.github.com/manual/gh_api).

Before declaring active: run the checker in the real worker environment, verify no prompts and read-only repository access, verify the installed revision, test conditional silence and a synthetic notification/ack with the host's supported testing facilities, and read back exactly one weekly job. A local script test is not proof of host notification delivery. Do not fake an update by changing the customer's install metadata. Machines must be awake and the scheduler running; use its supported missed-run handling.

When updating, follow INSTALL.md: back up private data, pause relevant publishing workers, use `--update` with explicit host/directory/workspace, then audit/reconcile before resuming. Preserve this job and private checker state. Cancellation removes only the update checker. Repository access alone does not authorize automatic installation.
