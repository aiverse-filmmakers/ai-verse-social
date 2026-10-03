from __future__ import annotations

import importlib.util
import os
import shutil
import time

from .util import UserError, now, write_json


def audit(engine, *, live=False, budget=120):
    """Known-path checks only. No recursive searches and no remote writes."""
    started = time.monotonic()
    checks = []
    def add(component, status, next_action="", detail=None):
        checks.append({"component": component, "status": status, "next_action": next_action, "detail": detail})
    config = engine.workspace.config
    engine.config=config
    add("workspace", "ready", detail=str(engine.workspace.path))
    private=(engine.workspace.path.stat().st_mode & 0o077)==0 if os.name=="posix" else False
    add("workspace permissions", "ready" if private else "needs_setup",
        "Restrict this folder to the customer account; verify Windows ACLs if applicable.")
    add("database", "ready", detail="SQLite quick-check passed")
    restore_pending=engine.store.meta("restore_reconciliation_required") == "1"
    add("restore reconciliation", "needs_setup" if restore_pending else "ready",
        "Review provider history for every connected account and run restore-reconcile." if restore_pending else "")
    for tool in ("ffmpeg", "ffprobe"):
        add(tool, "ready" if shutil.which(tool) else "missing", "Install FFmpeg in this host." if not shutil.which(tool) else "")
    add("provider secret", "ready" if os.environ.get("ZERNIO_API_KEY") else "missing", "Connect Zernio through your host secret store." if not os.environ.get("ZERNIO_API_KEY") else "")
    add("profile scope", "ready" if config["profiles"] else "missing", "List profiles and select the customer's profile(s)." if not config["profiles"] else "")
    destinations = engine.store.rows("SELECT * FROM destinations WHERE enabled=1")
    add("included accounts", "ready" if destinations else "missing", "Sync accounts and select the intended destinations." if not destinations else "")
    import json
    for account in destinations:
        capability = json.loads(account["capability"])
        status = "ready" if account["profile_id"] in config["profiles"] and account["connected"] and capability.get("video") and capability.get("evidence") else "needs_setup"
        add(f"account:{account['id']}", status, "Confirm connection health and video capabilities." if status != "ready" else "", account["label"])
    mode = config["captions"]["mode"]
    add("caption preferences", "ready" if config["captions"].get("voice") or mode == "custom" else "needs_setup",
        "Ask for caption mode, voice, an example, and CTA; preserve user wording.")
    add("transcription", "optional" if mode not in {"transcript","hybrid"} else "ready" if shutil.which("whisper-cli") or importlib.util.find_spec("faster_whisper") else "missing",
        "Supply a host transcript, install local transcription, or choose idea/custom mode.")
    if config["drive"]["enabled"]:
        has_library = importlib.util.find_spec("googleapiclient") is not None
        add("Drive adapter", "ready" if has_library else "missing", "Install the drive extra." if not has_library else "")
        add("Drive folders", "ready" if config["drive"]["folders"].get("ready") and config["drive"]["folders"].get("posted") else "needs_setup", "Map existing Ready/Posted folders or approve a new managed folder set.")
        request_archive=engine.store.one("SELECT 1 FROM assets WHERE library=0 AND drive_id IS NOT NULL LIMIT 1")
        request_archive_status="ready" if config["drive"]["folders"].get("request_scoped") else ("needs_setup" if request_archive else "optional")
        add("request-scoped archive",request_archive_status,"Map a request-scoped subfolder under Posted to keep one-platform-only videos distinct from library coverage.")
    else:
        add("Drive", "optional", "Enable Drive for cloud intake/archive. Chat/local input can work independently.")
    add("recurring permission", "ready" if config["approval"]["recurring_authorized"] else "needs_setup", "Record approved recurring scope before enabling automated posts.")
    add("scheduler", "needs_setup" if config["schedule"]["enabled"] else "optional", "Verify a host scheduler/worker invocation; settings alone do not start jobs.")
    editing_ready=bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
    from pathlib import Path
    for key in ("watermark","subtitles"):
        path=config["editing"]["options"].get(key)
        if path:
            exists=isinstance(path,str) and Path(path).expanduser().is_file()
            editing_ready=editing_ready and exists
            add("editing asset:"+key,"ready" if exists else "missing","Restore or configure the customer-supplied branding/subtitle file." if not exists else "")
    selected=engine.store.rows("SELECT * FROM destinations WHERE enabled=1 ORDER BY id")
    drive_ops=(1+len(config["drive"]["folders"])) if config["drive"]["enabled"] else 0
    health_ops=max(1,len(config["profiles"]))
    original_timeout=None
    if live:
        try:
            if not engine.provider:
                raise UserError("Provider is not configured.")
            profile_ops=max(1,len(config["profiles"]))
            original_timeout=getattr(engine.provider,"timeout",60)
            remaining=max(1,budget-(time.monotonic()-started))
            engine.provider.timeout=min(original_timeout,max(1,remaining/(profile_ops+health_ops+drive_ops)))
            listed=engine.provider.accounts(config["profiles"])
            if not isinstance(listed,list) or any(not isinstance(row,dict) or not row.get("_id") or not row.get("platform") for row in listed):
                raise UserError("Provider account listing lacks stable account identity; local records were not changed.")
            by_id={row["_id"]:row for row in listed}
            add("provider connection", "ready", detail=f"Read-only account listing succeeded ({len(listed)} accounts)")
            try:
                remaining=max(1,budget-(time.monotonic()-started))
                engine.provider.timeout=min(original_timeout,max(1,remaining/(health_ops+drive_ops)))
                health_rows=engine.provider.health_all(config["profiles"])
                health_by_id={row["accountId"]:row for row in health_rows}
            except UserError as exc:
                health_by_id={}
                add("provider health inventory","missing",str(exc))
            for account in selected:
                current=by_id.get(account["provider_id"])
                if current is None:
                    add(f"account inventory:{account['id']}","needs_setup","Refresh accounts or confirm whether this account was reconnected.",account["label"])
                    continue
                profile=current.get("profileId")
                profile=profile.get("_id") if isinstance(profile,dict) else profile
                native=current.get("platformUserId")
                native_key=f"{profile}:{current.get('platform')}:{native}" if native else None
                if profile!=account["profile_id"] or current.get("platform")!=account["platform"] or (account["native_key"] and native_key!=account["native_key"]):
                    add(f"account inventory:{account['id']}","needs_setup","Saved profile/native identity differs; resolve before publishing.")
                    continue
                if current.get("isActive") is not True:
                    add(f"account health:{account['id']}","needs_setup","Reconnect this account in Zernio before publishing.",account["label"])
                    continue
                health=health_by_id.get(account["provider_id"])
                if health is None:
                    add(f"account health:{account['id']}","needs_setup","Read-only publishing health could not be confirmed for this account.",account["label"])
                    continue
                can_post=health.get("canPost")
                healthy=health.get("status") == "healthy" and can_post is True
                detail={"status":health.get("status"),"can_post":can_post,"issues":health.get("issues") or []}
                add(f"account health:{account['id']}","ready" if healthy else "needs_setup",
                    "Reconnect or restore required publishing permissions in Zernio." if not healthy else "",
                    {"account":account["label"],**detail})
        except UserError as exc:
            add("provider connection", "missing", str(exc))
        if config["drive"]["enabled"] and time.monotonic()-started < budget:
            try:
                from .drive import Drive
                remaining=max(1,budget-(time.monotonic()-started))
                drive_timeout=min(60,max(1,remaining/max(1,drive_ops)))
                drive = Drive(config["drive"],timeout=drive_timeout,refresh_credentials=False,retry_attempts=1)
                drive.identity()
                for key, value in config["drive"]["folders"].items():
                    if time.monotonic()-started >= budget:
                        add("audit budget", "pending", "Resume the remaining folder checks.")
                        break
                    folder = drive.metadata(value)
                    if folder.get("mimeType") != "application/vnd.google-apps.folder" or folder.get("trashed"):
                        raise UserError(f"Mapped {key} is not an available Drive folder.")
                    add(f"Drive:{key}", "ready")
            except UserError as exc:
                add("Drive connection", "missing", str(exc))
            except Exception:
                add("Drive connection", "missing", "Check credentials, folder permissions, and Drive diagnostics.")
        if engine.provider and original_timeout is not None:
            engine.provider.timeout=original_timeout
    else:
        add("live connection checks", "not_tested", "Run audit --live for read-only provider/Drive checks.")
    required = {"ffmpeg", "ffprobe", "provider secret", "profile scope", "included accounts", "workspace permissions"}
    base_ready = all(c["status"] == "ready" for c in checks if c["component"] in required)
    base_ready = base_ready and all(c["status"] == "ready" for c in checks if c["component"].startswith("account:"))
    if live:
        live_required = ("provider connection", "account inventory:", "account health:")
        base_ready = base_ready and all(
            check["status"] == "ready"
            for check in checks
            if check["component"] == live_required[0] or check["component"].startswith(live_required[1:])
        )
    mode_state = ("restore_reconciliation_required" if restore_pending else "paused" if config["paused"]
                  else "live_check_required" if not live else "configured" if base_ready else "needs_setup")
    recurring_state = "restore_reconciliation_required" if restore_pending else "paused" if config["paused"] else "needs_scheduler_verification"
    result = {"checked_at": now(), "read_only": True, "checks": checks,
              "modes": {"live_publishing": mode_state,
                        "recurring": recurring_state, "editing": "configured" if editing_ready else "missing"},
              "next_step": next((c for c in checks if c["status"] in {"missing", "needs_setup", "not_tested"}), None)}
    write_json(engine.workspace.path / "onboarding-audit.json", result)
    return result
