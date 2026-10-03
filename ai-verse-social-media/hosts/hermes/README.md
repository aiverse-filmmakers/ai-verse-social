# Hermes setup

Install the complete `ai-verse-social-media` package under Hermes' configured skill root, preserving its folder name. Also copy either or both folders under `skills/` here into that same skill root. They are tiny slash-command aliases that route onboarding into the main package rather than duplicating its rules. Restart/reload skills and verify `/ai-verse-social-media`, `/ai-verse-social-media-onboard`, and `/ai-verse-social-media-oboard` appear in the target Hermes version.

Set a persistent customer-private `AI_VERSE_SOCIAL_WORKSPACE` and store `ZERNIO_API_KEY` in Hermes' approved secret/environment mechanism for the scheduled worker. Use `python3.11 ABSOLUTE_SKILL/scripts/social.py --workspace "$AI_VERSE_SOCIAL_WORKSPACE" audit --live` during guided setup. Do not place secrets in a cron prompt.

## Bounded recurring job

After the customer enables `schedule.enabled` and explicitly records recurring scope (`approval.recurring_authorized=true`), ask Hermes to create a skill-backed recurring job. For example:

```text
/cron add every weekday at 9:00 "Follow the AI-Verse social media skill's recurring operations. In the configured workspace, run one bounded Drive intake page (maximum 3 new videos), then select only eligible library assets and the explicitly approved destination accounts. Prepare platform-specific captions using saved caption context, show/record any required review, and authorize only inside the customer's recorded recurring scope. Submit or reconcile the already-authorized jobs with tick. Move Drive originals to Posted only after included-account coverage is verified. Save a short run receipt and continue next run if the time budget is reached." --skill ai-verse-social-media
```

This command is an example for Hermes versions whose cron interface accepts `/cron add` and `--skill`. Verify the job through Hermes' cron list/read-back before claiming it is active. The scheduled worker can use the agent model for selection and caption writing; `tick` itself only submits or reconciles prepared requests. Keep job frequency and batch size within the customer's approved cadence and provider quotas. A fresh-session cron needs filesystem, Python, FFmpeg and secret access to the same workspace.

An onboarding audit never creates this job automatically. Scheduled posts already accepted by Zernio may still publish if Hermes is paused or offline.
