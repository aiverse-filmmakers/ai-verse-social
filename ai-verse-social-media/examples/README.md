# Private setup examples

`settings.empty.json` is valid, deliberately unconnected configuration. Copy into a new private workspace after initialization and customize through onboarding. Never overwrite existing customer settings with this file.

`request.template.json` shows the manifest shape; replace its descriptive IDs with actual ingestion/account results and supply approved platform fields. It is not a ready publication or a list of supported platforms. `media_by_account` optionally maps exact destination IDs to validated workspace rendition paths, permitting different crops/lengths for different accounts.

`editing.standard.json` demonstrates a 1080 × 1920 padded rendition and audio normalization. It does not crop away source content, invent captions, or apply a default customer logo. Put optional branding assets inside the private workspace `branding/` folder for relocation-safe backups. Use passthrough to preserve the source encoding.

Capability contracts intentionally have no universal populated example: collect current video support, limits, required fields and final-publication evidence for the customer's actual account before enabling it.

The empty settings enable sampled repeat protection. Map `drive.folders.repeat_review` only to an approved review folder. Record visual-check opt-out with `repeat-policy`; do not paste customer footage/history into this template.
