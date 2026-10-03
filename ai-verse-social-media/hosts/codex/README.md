# Codex setup

Install the complete folder as a local Codex skill. The skill name provides the primary route; use natural language for onboarding if the slash palette does not expose a separate onboarding alias. Recurring operation requires a host automation or persistent worker with filesystem, Python 3.11+, FFmpeg, the same private workspace, and secrets. Configure recurring scope before the worker runs and verify its first run. A scheduled automation that only runs the `tick` command will not discover videos or draft captions; it needs to invoke the agent with this skill loaded.
