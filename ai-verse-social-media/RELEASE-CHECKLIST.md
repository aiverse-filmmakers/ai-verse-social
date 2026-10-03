# Finite release acceptance checklist

The offline audit/build is complete when the regression suite, real-media fixture, skill metadata validator, clean ZIP extraction and installed CLI smoke test pass. These are engineering checks, not live platform certification.

Before advertising a host/account combination as tested, run exactly this small customer-authorized acceptance flow:

1. Load the skill and onboarding alias in that host. Initialize a new private workspace; verify secrets, persistent attachment access and FFmpeg availability.
2. Connect selected Zernio profiles and Drive folders. Run read-only live audit; record native identity and current capability fields/proof for each enabled account.
3. Ingest one short authorized video. Publish to one approved destination and then the remaining approved destinations; verify actual captions, final identifiers/URLs and folder closure. Public publication needs the user's permission.
4. Add one account and verify archive backfill selects it while previously verified accounts remain excluded. Exercise a scoped live request separately.
5. Schedule one approved future post, confirm its saved timing, and test cancellation before dispatch. Test an existing draft promotion and failed-post recovery only with appropriate fixtures/provider approval.
6. Verify the host's recurring worker in a fresh session, one bounded intake/prepare/tick cycle and one restart/backup/restore. Inspect post-backup provider history before clearing quarantine.

Record date, host/version, dependency versions, tested account/platform route and actual evidence in a private customer test report. Publish support claims only for combinations that pass. No additional product rebuild is implied by these account-specific checks.

Before public sale, the owner must select purchaser terms (who can use/modify/resell, installation limits, update/support policy) and review notices for the actual distributed runtime. No customer license grant has been invented here. See VERIFICATION.md and THIRD-PARTY-NOTICES.md.
