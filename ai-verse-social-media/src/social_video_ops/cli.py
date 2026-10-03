from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import os
import sys
import time
import uuid
from pathlib import Path

from .audit import audit
from .backup import create_backup, restore_backup
from .config import Workspace
from .engine import Engine
from .providers import Zernio
from .util import UserError, read_json, write_json, now


def parser():
    p = argparse.ArgumentParser(description="AI-Verse social video operations; one engine for live and scheduled work")
    p.add_argument("--workspace", default=os.environ.get("AI_VERSE_SOCIAL_WORKSPACE"), help="Customer-private workspace outside the installed skill")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    a = sub.add_parser("audit"); a.add_argument("--live", action="store_true")
    a = sub.add_parser("configure"); a.add_argument("file", type=Path)
    sub.add_parser("profiles")
    a = sub.add_parser("accounts"); a.add_argument("--sync", action="store_true")
    a = sub.add_parser("account-policy"); a.add_argument("destination"); a.add_argument("--enabled", choices=["yes","no"]); a.add_argument("--capability", type=Path)
    a = sub.add_parser("drive-archive-policy"); a.add_argument("--enabled", choices=["yes","no"], required=True)
    a = sub.add_parser("ingest"); a.add_argument("file", type=Path); a.add_argument("--library", action="store_true")
    a = sub.add_parser("enroll"); a.add_argument("asset"); a.add_argument("--library", choices=["yes","no"], required=True)
    a = sub.add_parser("drive-connect"); a.add_argument("client_file", type=Path); a.add_argument("token_file", type=Path)
    a = sub.add_parser("drive-inventory"); a.add_argument("folder", help="Configured folder key")
    a = sub.add_parser("drive-ingest"); a.add_argument("file_id"); a.add_argument("--library", action="store_true")
    a = sub.add_parser("drive-intake"); a.add_argument("--limit",type=int,default=3); a.add_argument("--library",action="store_true")
    a = sub.add_parser("drive-sync"); a.add_argument("--asset")
    a = sub.add_parser("edit"); a.add_argument("asset"); a.add_argument("--recipe", type=Path)
    a = sub.add_parser("transcribe"); a.add_argument("asset"); a.add_argument("--model-path"); a.add_argument("--language")
    a = sub.add_parser("transcript-import"); a.add_argument("asset"); a.add_argument("file", type=Path)
    a = sub.add_parser("caption-add"); a.add_argument("file", type=Path); a.add_argument("--kind", required=True); a.add_argument("--asset"); a.add_argument("--destination"); a.add_argument("--parent"); a.add_argument("--note", default="")
    a = sub.add_parser("caption-context"); a.add_argument("--asset"); a.add_argument("--destination")
    a = sub.add_parser("prepare"); a.add_argument("file", type=Path)
    a = sub.add_parser("authorize"); a.add_argument("request"); a.add_argument("payload_hash"); a.add_argument("--note-file", type=Path, required=True)
    a = sub.add_parser("execute"); a.add_argument("job")
    a = sub.add_parser("cancel"); a.add_argument("job")
    a = sub.add_parser("recover"); a.add_argument("job"); a.add_argument("operation",choices=["reconcile","retry-failed","promote-draft"]); a.add_argument("--note-file",type=Path,required=True); a.add_argument("--provider-id"); a.add_argument("--request"); a.add_argument("--payload-hash")
    a = sub.add_parser('repeat-policy'); a.add_argument('--enabled',choices=['yes','no'],required=True); a.add_argument('--note-file',type=Path,required=True)
    a = sub.add_parser('repeat-check'); a.add_argument('asset'); a.add_argument('--accounts', default='all')
    a = sub.add_parser('repeat-index'); a.add_argument('--limit', type=int, default=3)
    a = sub.add_parser('repeat-review'); a.add_argument('asset'); a.add_argument('--decision', choices=['same','distinct'], required=True); a.add_argument('--match', default=''); a.add_argument('--note-file',type=Path,required=True)
    a = sub.add_parser('repeat-panel'); a.add_argument('--asset')
    sub.add_parser('repeat-sync')
    a = sub.add_parser('repeat-restore'); a.add_argument('source')
    sub.add_parser("tick")
    a = sub.add_parser("status"); a.add_argument("--asset"); a.add_argument("--limit",type=int,default=100); a.add_argument("--offset",type=int,default=0)
    a = sub.add_parser("next-times"); a.add_argument("destination"); a.add_argument("--analytics", action="store_true")
    sub.add_parser("export")
    a = sub.add_parser("pause"); a.add_argument("--resume", action="store_true")
    a = sub.add_parser("restore-reconcile"); a.add_argument("--note-file", type=Path, required=True)
    a = sub.add_parser("backup"); a.add_argument("target", type=Path)
    a = sub.add_parser("restore"); a.add_argument("backup", type=Path); a.add_argument("--to", dest="target", type=Path, required=True)
    return p


def dispatch(args, engine):
    command = args.command
    if command == 'repeat-policy': return engine.repeat.set_policy(args.enabled=='yes',args.note_file.read_text())
    if command == 'repeat-check':
        return engine.repeat.check(args.asset,[d['id'] for d in engine.targets(args.accounts)])
    if command == 'repeat-index': return engine.repeat.index(args.limit)
    if command == 'repeat-review': return engine.repeat.review(args.asset,args.decision,args.match,args.note_file.read_text())
    if command == 'repeat-panel': return engine.repeat.panel(args.asset)
    if command == 'repeat-sync': return engine.repeat.sync_sources()
    if command == 'repeat-restore': return engine.repeat.sync_sources(restore=args.source)
    if command == "audit":
        return audit(engine, live=args.live)
    if command == "configure":
        updated=read_json(args.file)
        if updated.get('repeat_guard',{}).get('enabled') is False and engine.workspace.config['repeat_guard']['enabled']:
            raise UserError('Use repeat-policy --enabled no --note-file FILE to record disabling visual checks.')
        engine.workspace.save(updated)
        return {"configured": True, "next": "audit"}
    if command == "profiles":
        return engine.provider.profiles()
    if command == "accounts":
        if args.sync:
            config = engine.config
            result = engine.store.sync_accounts(engine.provider.accounts(config["profiles"]), config["profiles"],
                      auto_enroll=config["accounts"]["auto_enroll"], future_backfill=config["accounts"]["future_backfill"])
            engine.export_logs()
            return {**result, "accounts": engine.store.rows("SELECT * FROM destinations")}
        return {"accounts": engine.store.rows("SELECT * FROM destinations")}
    if command == "account-policy":
        engine.store.set_destination(args.destination, enabled=None if args.enabled is None else args.enabled == "yes",
                                     capability=read_json(args.capability) if args.capability else None,future_backfill=engine.config["accounts"]["future_backfill"])
        engine.export_logs()
        return {"updated": args.destination}
    if command == "drive-archive-policy":
        config=engine.workspace.config
        config["drive"]["archive_local_uploads"] = args.enabled == "yes"
        engine.workspace.save(config)
        return {"archive_local_uploads": config["drive"]["archive_local_uploads"],
                "note":"This changes future filing behavior. Run drive-sync to apply it to eligible verified videos."}
    if command == "ingest":
        return engine.ingest(args.file, library=args.library)
    if command == "enroll":
        engine.enroll(args.asset, args.library == "yes")
        return engine.store.coverage(args.asset)
    if command == "drive-connect":
        from .drive import connect
        result = connect(args.client_file, args.token_file)
        config = engine.config
        config["drive"]["credentials_file"] = str(args.token_file.expanduser().resolve())
        config["drive"]["enabled"] = True
        engine.workspace.save(config)
        return result
    if command == "drive-inventory":
        from .drive import Drive
        folder = engine.config["drive"]["folders"].get(args.folder)
        if not folder:
            raise UserError("Map that Drive folder during onboarding first.")
        return {"files": Drive(engine.config["drive"]).inventory(folder)}
    if command == "drive-ingest":
        from .drive import Drive
        drive = Drive(engine.config["drive"])
        metadata = drive.metadata(args.file_id)
        allowed = set(engine.config["drive"]["folders"].values())
        parents = set(metadata.get("parents", []))
        if not parents.intersection(allowed):
            raise UserError("Drive file is outside the customer's configured source/archive folders.")
        suffix = Path(metadata["name"]).suffix.lower()
        if suffix not in {".mp4", ".mov", ".webm", ".m4v", ".avi"}:
            raise UserError("Unsupported video file extension; verify the actual media.")
        safe_id=hashlib.sha256(args.file_id.encode("utf-8")).hexdigest()
        download = engine.workspace.path / "media" / "originals" / ("drive-"+safe_id+"-"+uuid.uuid4().hex+suffix)
        drive.download(args.file_id, download)
        result=engine.ingest(download, library=args.library, drive_id=args.file_id,
                             drive_parent=next(iter(parents.intersection(allowed))),drive_version=metadata.get("version"))
        download.unlink(missing_ok=True)
        return result
    if command == "drive-intake":
        if not 1 <= args.limit <= 10:
            raise UserError("Drive intake limit must be between 1 and 10 videos per run.")
        config=engine.config["drive"]
        folder=config.get("folders",{}).get("ready")
        if not config.get("enabled") or not folder:
            raise UserError("Connect Drive and map the customer's Ready folder first.")
        from .drive import Drive
        drive=Drive(config)
        cursor_key="drive-intake-cursor:"+folder
        watermark_key="drive-intake-watermark:"+folder
        scan_start_key="drive-intake-scan-start:"+folder
        token=engine.store.meta(cursor_key)
        watermark=engine.store.meta(watermark_key)
        if token is None:
            scan_start=now()
            engine.store.set_meta(scan_start_key,scan_start)
            from datetime import datetime,timedelta
            cutoff=(datetime.fromisoformat(watermark.replace("Z","+00:00"))-timedelta(minutes=2)).isoformat() if watermark else None
        else:
            scan_start=engine.store.meta(scan_start_key) or now()
            cutoff=None if not watermark else (datetime.fromisoformat(watermark.replace("Z","+00:00"))-timedelta(minutes=2)).isoformat()
        files,next_token=drive.inventory_page(folder,token,limit=100,modified_after=cutoff)
        queued=[]
        with engine.store.transaction():
            for item in files:
                if item.get("mimeType","").startswith("video/"):
                    engine.store.db.execute("""INSERT INTO drive_intake(file_id,name,version,size,modified,state,updated)
                        VALUES(?,?,?,?,?,'pending',?) ON CONFLICT(file_id) DO UPDATE SET
                        name=excluded.name,size=excluded.size,modified=excluded.modified,
                        state=CASE WHEN drive_intake.version IS NOT excluded.version THEN 'needs_review' ELSE drive_intake.state END,
                        error=CASE WHEN drive_intake.version IS NOT excluded.version THEN 'Drive file changed after discovery; review the latest revision.' ELSE drive_intake.error END,
                        version=excluded.version,updated=excluded.updated""",
                        (item["id"],item.get("name","video"),item.get("version"),int(item.get("size",0) or 0),item.get("modifiedTime"),now()))
            engine.store.set_meta(cursor_key,next_token)
            if next_token is None:
                engine.store.set_meta(watermark_key,scan_start)
                engine.store.set_meta(scan_start_key,None)
        candidates=engine.store.rows("SELECT * FROM drive_intake WHERE state='pending' OR (state='processing' AND COALESCE(lease_until,0)<?) ORDER BY modified,file_id LIMIT ?",(time.time(),args.limit))
        for candidate in candidates:
            owner=str(uuid.uuid4())
            with engine.store.transaction():
                claimed=engine.store.db.execute("UPDATE drive_intake SET state='processing',lease_owner=?,lease_until=?,updated=? WHERE file_id=? AND (state='pending' OR (state='processing' AND COALESCE(lease_until,0)<?))",
                    (owner,time.time()+14400,now(),candidate["file_id"],time.time()))
                item=engine.store.one("SELECT * FROM drive_intake WHERE file_id=?",(candidate["file_id"],)) if claimed.rowcount else None
            if not item:
                continue
            try:
                metadata=drive.metadata(item["file_id"])
                if metadata.get("version") != item["version"]:
                    engine.store.db.execute("UPDATE drive_intake SET state='needs_review',lease_owner=NULL,lease_until=NULL,error='Drive file changed after discovery; review the latest revision.',updated=? WHERE file_id=? AND lease_owner=?",(now(),item["file_id"],owner))
                    queued.append({"file_id":item["file_id"],"state":"needs_review"}); continue
                suffix=Path(metadata["name"]).suffix.lower()
                if suffix not in {".mp4",".mov",".webm",".m4v",".avi"}:
                    engine.store.db.execute("UPDATE drive_intake SET state='unsupported',lease_owner=NULL,lease_until=NULL,error='Unsupported video extension.',updated=? WHERE file_id=? AND lease_owner=?",(now(),item["file_id"],owner))
                    queued.append({"file_id":item["file_id"],"state":"unsupported"}); continue
                safe_id=hashlib.sha256(item["file_id"].encode("utf-8")).hexdigest()
                target=engine.workspace.path/"media"/"originals"/("drive-"+safe_id+"-"+uuid.uuid4().hex+suffix)
                drive.download(item["file_id"],target)
                parents=set(metadata.get("parents",[]))
                if folder not in parents:
                    raise UserError("Drive file left the configured Ready folder during intake.")
                result=engine.ingest(target,library=args.library,drive_id=item["file_id"],drive_parent=folder,drive_version=metadata.get("version"))
                target.unlink(missing_ok=True)
                with engine.store.transaction():
                    engine.store.db.execute("UPDATE drive_intake SET state='ingested',asset_id=?,lease_owner=NULL,lease_until=NULL,error=NULL,updated=? WHERE file_id=? AND lease_owner=?",
                        (result["asset"]["id"],now(),item["file_id"],owner))
                queued.append({"file_id":item["file_id"],"asset_id":result["asset"]["id"],"state":"ingested"})
            except (UserError,OSError) as exc:
                engine.store.db.execute("UPDATE drive_intake SET state='pending',lease_owner=NULL,lease_until=NULL,error=?,updated=? WHERE file_id=? AND lease_owner=?",(str(exc),now(),item["file_id"],owner))
                queued.append({"file_id":item["file_id"],"state":"retry_pending","error":str(exc)})
        engine.export_logs()
        return {"discovered":len(files),"more_pages":bool(next_token),"processed":queued,"queued_pending":len(engine.store.rows("SELECT file_id FROM drive_intake WHERE state='pending'"))}
    if command == "drive-sync":
        return engine.sync_drive(args.asset)
    if command == "edit":
        from .media import render
        asset = engine.store.require_asset(args.asset)
        return render(Path(asset["path"]), engine.workspace.path, read_json(args.recipe) if args.recipe else engine.config["editing"])
    if command == "transcribe":
        from .media import transcribe
        asset = engine.store.require_asset(args.asset)
        result = transcribe(Path(asset["path"]), engine.workspace.path, model_path=args.model_path, language=args.language)
        engine.store.db.execute("UPDATE assets SET transcript=? WHERE id=?", (result["text"], args.asset))
        engine.store.event("transcript", args.asset, {"source": result["source"], "sha256": result["sha256"]})
        return result
    if command == "transcript-import":
        engine.store.require_asset(args.asset)
        text = args.file.read_text().strip()
        if not text:
            raise UserError("Transcript file is empty.")
        engine.store.db.execute("UPDATE assets SET transcript=? WHERE id=?", (text, args.asset))
        engine.store.event("transcript", args.asset, {"source": "user/host supplied"})
        return {"asset": args.asset, "characters": len(text)}
    if command == "caption-add":
        return {"entry_id": engine.captions.add(args.file.read_text(), args.kind, asset_id=args.asset,
                destination_id=args.destination, parent_id=args.parent, note=args.note), "journal": str(engine.workspace.path / "captions" / "caption-journal.md")}
    if command == "caption-context":
        return engine.captions.context(engine.config, args.asset, args.destination)
    if command == "prepare":
        return engine.prepare(read_json(args.file))
    if command == "authorize":
        return engine.authorize(args.request, args.payload_hash, args.note_file.read_text())
    if command == "execute":
        return engine.execute(args.job)
    if command == "cancel":
        return engine.cancel_job(args.job)
    if command == "recover":
        return engine.recover(args.job,args.operation,args.note_file.read_text(),provider_id=args.provider_id,request_id=args.request,payload_hash=args.payload_hash)
    if command == "tick":
        return engine.tick()
    if command == "status":
        if args.asset:
            return engine.store.coverage(args.asset)
        if not 1<=args.limit<=500 or args.offset<0:
            raise UserError("Status pagination requires limit 1–500 and a nonnegative offset.")
        assets=engine.store.rows("SELECT id,title,state,library FROM assets ORDER BY created DESC,id LIMIT ? OFFSET ?",(args.limit,args.offset))
        for asset in assets:
            asset['work_id']=engine.store.work_id(asset['id'])
            asset['repeat_hold']=engine.store.one('SELECT status,data FROM repeat_holds WHERE asset_id=?',(asset['id'],))
            scope=engine.store.coverage(asset['id'])
            eligible=[r['destination_id'] for r in scope['destinations'] if r['enabled'] and r['connected'] and not engine.store.work_job(asset['id'],r['destination_id'])]
            asset['eligible_accounts']=eligible
            asset['automatic_selection_eligible']=bool(asset['library'] and eligible and asset['repeat_hold'] is None)
        ids=[asset["id"] for asset in assets]
        if ids:
            placeholders=",".join("?" for _ in ids)
            jobs=engine.store.rows(f"SELECT id,asset_id,destination_id,state,due,provider_id,error FROM jobs WHERE asset_id IN ({placeholders}) ORDER BY updated",ids)
        else:
            jobs=[]
        return {"paused": engine.config["paused"],
                "restore_reconciliation_required": engine.store.meta("restore_reconciliation_required") == "1",
                "assets": assets,
                "total_assets": engine.store.one("SELECT COUNT(*) AS n FROM assets")["n"],"offset":args.offset,"limit":args.limit,
                "coverage": [engine.store.coverage(asset["id"]) for asset in assets],
                "requests": [engine.request_coverage(row["id"]) for row in engine.store.rows(f"SELECT id FROM requests WHERE asset_id IN ({','.join('?' for _ in ids)}) ORDER BY created DESC LIMIT 100",ids)] if ids else [],
                "jobs": jobs,
                "derived_export_error": engine.store.meta("derived_export_error"),
                "drive_intake": engine.store.rows("SELECT file_id,name,state,error,asset_id,updated FROM drive_intake WHERE state!='ingested' ORDER BY updated LIMIT 100"),
                "sync_actions": engine.store.rows("SELECT * FROM actions WHERE state!='done' LIMIT 100"),
                "drive_exports": engine.store.rows("SELECT * FROM drive_exports WHERE state!='filed' LIMIT 100")}
    if command == "next-times":
        from .scheduling import next_times
        destination = engine.store.one("SELECT * FROM destinations WHERE id=?", (args.destination,))
        if not destination:
            raise UserError("Unknown destination.")
        booked = [row["due"] for row in engine.store.rows("SELECT due FROM jobs WHERE destination_id=? AND state NOT IN ('cancelled','failed','provider_draft')", (args.destination,))]
        analytics = None
        if args.analytics:
            try:
                result = engine.provider.best_times(destination["provider_id"])
                analytics = result.get("slots") or result.get("bestTimes") or []
            except UserError:
                pass
        return next_times(engine.config, booked, analytics=analytics)
    if command == "export":
        engine.export_logs()
        engine.captions.export()
        return {"exported": True}
    if command == "pause":
        config = engine.config
        if args.resume and engine.store.meta("restore_reconciliation_required") == "1":
            raise UserError("Restore reconciliation is still required. Review provider history and run restore-reconcile first.")
        config["paused"] = not args.resume
        engine.workspace.save(config)
        return {"paused": config["paused"], "note": "Provider-scheduled posts can still execute; inspect outstanding schedules before cancellation."}
    if command == "restore-reconcile":
        return engine.acknowledge_restore_reconciliation(args.note_file.read_text(encoding="utf-8"))
    if command == "backup":
        return create_backup(engine, args.target)
    raise UserError("Unknown operation.")


def main(argv=None):
    args = parser().parse_args(argv)
    engine = None
    try:
        if args.command == "restore":
            print(json.dumps(restore_backup(args.backup, args.target), indent=2))
            return 0
        if not args.workspace:
            raise UserError("Pass --workspace or set AI_VERSE_SOCIAL_WORKSPACE.")
        workspace = Workspace(args.workspace)
        if args.command == "init":
            workspace.init()
            engine = Engine(workspace)
            print(json.dumps({"initialized": True, "workspace": str(workspace.path), "next": "audit"}, indent=2))
            return 0
        network = args.command in {"profiles", "execute", "tick", "restore-reconcile", "cancel", "recover"} or (args.command == "accounts" and args.sync) or (args.command == "audit" and args.live) or (args.command == "next-times" and args.analytics)
        provider = None
        if network and os.environ.get("ZERNIO_API_KEY"):
            provider = Zernio()
        engine = Engine(workspace, provider)
        print(json.dumps(dispatch(args, engine), ensure_ascii=False, indent=2))
        return 0
    except sqlite3.Error:
        print(json.dumps({"ok":False,"error":"Local state storage failed; preserve this workspace and reconcile any provider attempt before retrying."}),file=sys.stderr)
        return 2
    except (UserError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    finally:
        if engine:
            engine.close()


if __name__ == "__main__":
    raise SystemExit(main())
