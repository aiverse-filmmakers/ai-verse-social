from __future__ import annotations

import io
import csv
import json
import os
import re
import shutil
import time
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .captions import CaptionMemory, validate_text
from .config import Workspace, validate_capability
from .media import probe
from .providers import ProviderError, extract_post
from .store import Store
from .util import UserError, atomic_write, canonical, digest, file_hash, https_url, now, timestamp, read_json


class Engine:
    def __init__(self, workspace: Workspace, provider=None):
        self.workspace = workspace
        self.config = workspace.config
        self.store = Store(workspace.path / "state.sqlite3")
        self.provider = provider
        self.captions = CaptionMemory(self.store, workspace.path)

    def close(self):
        self.store.close()

    def ingest(self, source: Path, *, library=False, drive_id=None, drive_parent=None, drive_version=None) -> dict:
        source = source.expanduser().resolve()
        qc = probe(source)
        found = self.store.one("SELECT * FROM assets WHERE hash=?", (qc["sha256"],))
        if found:
            # Repeated uploads do not silently override a request-only distribution policy.
            if drive_id:
                with self.store.transaction():
                    self.store.db.execute("INSERT OR IGNORE INTO asset_sources(asset_id,provider,source_id,parent,version,created) VALUES(?,?,?,?,?,?)",
                                          (found["id"],"gdrive",drive_id,drive_parent,drive_version,now()))
                    if not found["drive_id"]:
                        self.store.db.execute("UPDATE assets SET drive_id=?,drive_parent=? WHERE id=?",(drive_id,drive_parent,found["id"]))
            return {"asset": found, "duplicate": True}
        identity = str(uuid.uuid4())
        target = self.workspace.path / "media" / "originals" / f"{qc['sha256']}{source.suffix.lower()}"
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent,delete=False) as handle:
            temporary=Path(handle.name)
            try:
                with source.open("rb") as incoming: shutil.copyfileobj(incoming,handle)
                handle.flush()
                if file_hash(temporary)!=qc["sha256"]: raise UserError("Video changed during ingestion; try again once upload is finished.")
                os.replace(temporary,target)
            finally: temporary.unlink(missing_ok=True)
        with self.store.transaction():
            concurrent=self.store.one("SELECT * FROM assets WHERE hash=?",(qc["sha256"],))
            if concurrent:
                if drive_id:
                    self.store.db.execute("INSERT OR IGNORE INTO asset_sources(asset_id,provider,source_id,parent,version,created) VALUES(?,?,?,?,?,?)",(concurrent["id"],"gdrive",drive_id,drive_parent,drive_version,now()))
                return {"asset":concurrent,"duplicate":True}
            self.store.db.execute("INSERT INTO assets(id,hash,title,path,drive_id,drive_parent,library,created) VALUES(?,?,?,?,?,?,?,?)",
                                  (identity, qc["sha256"], source.name, str(target), drive_id, drive_parent, int(library), now()))
            if drive_id:
                self.store.db.execute("INSERT OR IGNORE INTO asset_sources(asset_id,provider,source_id,parent,version,created) VALUES(?,?,?,?,?,?)",
                                      (identity,"gdrive",drive_id,drive_parent,drive_version,now()))
            if library:
                destinations = [r["id"] for r in self.store.rows("SELECT id FROM destinations WHERE enabled=1")]
                self.store.add_obligations(identity, destinations)
            self.store.event("ingested", identity, {"sha256": qc["sha256"], "library": library, "qc": qc})
        self.export_logs()
        return {"asset": self.store.require_asset(identity), "duplicate": False}

    def enroll(self, asset_id: str, library: bool):
        self.store.require_asset(asset_id)
        with self.store.transaction():
            self.store.db.execute("UPDATE assets SET library=? WHERE id=?", (int(library), asset_id))
            if library:
                self.store.add_obligations(asset_id, [row["id"] for row in self.store.rows("SELECT id FROM destinations WHERE enabled=1")])
            self.store.event("distribution-policy", asset_id, {"library": library})

    def targets(self, selector: str | list[str]) -> list[dict]:
        self.config = self.workspace.config
        destinations = [row for row in self.store.rows("SELECT * FROM destinations WHERE enabled=1 ORDER BY platform,label,id") if row["profile_id"] in self.config["profiles"]]
        if selector == "all":
            if not destinations:
                raise UserError("No included accounts. Run onboarding and enable your accounts.")
            return destinations
        requested = [selector] if isinstance(selector, str) else selector
        if not isinstance(requested,list) or any(not isinstance(v,str) for v in requested): raise UserError("Accounts must be all, a destination/platform, or a list of exact destinations.")
        result = []
        for value in requested:
            matches = [row for row in destinations if row["id"] == value or row["platform"] == value]
            if len(matches) != 1:
                raise UserError(f"Account selection '{value}' is unavailable or ambiguous; use its exact destination ID.")
            if matches[0] not in result:
                result.append(matches[0])
        if not result:
            raise UserError("Select at least one included account.")
        return result

    def validate_schedule(self, destination_id: str, due: str, asset_id: str):
        planned=timestamp(due)
        timezone_name=self.config["timezone"]
        local=planned.astimezone(ZoneInfo(timezone_name))
        window=local.strftime("%H:%M")
        if window not in self.config["schedule"]["windows"]:
            raise UserError(f"{window} is outside the customer's approved posting times in {timezone_name}.")
        existing=self.store.rows("SELECT due,state,asset_id FROM jobs WHERE destination_id=? AND asset_id!=? AND state NOT IN ('cancelled','failed')",(destination_id,asset_id))
        same_day=[row for row in existing if timestamp(row["due"]).astimezone(ZoneInfo(timezone_name)).date()==local.date()]
        if len(same_day)>=self.config["schedule"]["max_posts_per_day"]:
            raise UserError("This account's approved daily post limit is already reserved for that local date.")
        gap=self.config["schedule"]["min_gap_minutes"]*60
        if any(abs((planned-timestamp(row["due"])).total_seconds())<gap for row in existing):
            raise UserError("This scheduled post is too close to another reservation for the account.")

    def prepare(self, specification: dict) -> dict:
        self.config = self.workspace.config
        if not isinstance(specification,dict): raise UserError("Publication specification must be an object.")
        asset = self.store.require_asset(specification.get("asset_id"))
        mode = specification.get("mode", "draft")
        if not isinstance(mode,str) or mode not in {"now", "schedule", "draft"}:
            raise UserError("Mode must be now, schedule, or draft.")
        for key in ("captions","media_by_account"):
            if key in specification and not isinstance(specification[key],dict): raise UserError(f"{key} must be an object.")
        selected = self.targets(specification.get("accounts", "all"))
        media = Path(specification.get("media") or asset["path"]).expanduser().resolve()
        # A rendition must be an engine-created workspace file, not an arbitrary new video.
        if media != Path(asset["path"]).resolve():
            if not media.is_relative_to(self.workspace.path / "media" / "ready"):
                raise UserError("Edited media must be a validated workspace rendition.")
            qc_path = media.with_suffix(".qc.json")
            if not qc_path.exists():
                raise UserError("Edited media needs its QC record.")
            record = read_json(qc_path)
            if not isinstance(record,dict) or record.get("source_sha256") != asset["hash"] or record.get("sha256") != file_hash(media) or not record.get("recipe_hash"):
                raise UserError("Edited media QC does not match this source and output revision.")
        qc = probe(media)
        payload = {"asset_id": asset["id"], "mode": mode, "media": str(media), "media_hash": qc["sha256"], "targets": {}}
        caption_mode = self.config["captions"]["mode"]
        if caption_mode == "custom" or self.config["captions"].get("ask_custom_every_time"):
            if not self.store.one("SELECT id FROM caption_entries WHERE asset_id=? AND kind IN ('custom','revision')", (asset["id"],)):
                raise UserError("This customer requests custom wording each time. Record their caption for this video first.")
        if caption_mode == "transcript" and not asset["transcript"]:
            raise UserError("Transcript caption mode needs the video's actual transcript first.")
        for account in selected:
            capability = validate_capability(json.loads(account["capability"]))
            target_media=Path(specification.get("media_by_account",{}).get(account["id"],str(media))).expanduser().resolve()
            if target_media != media:
                if not target_media.is_relative_to(self.workspace.path / "media" / "ready"):
                    raise UserError("Per-account media must be a workspace rendition.")
                record=read_json(target_media.with_suffix(".qc.json"))
                if not isinstance(record,dict) or record.get("source_sha256")!=asset["hash"] or record.get("sha256")!=file_hash(target_media) or not record.get("recipe_hash"):
                    raise UserError("Per-account rendition QC does not match the approved source.")
            target_qc=probe(target_media) if target_media!=media else qc
            if capability.get("video") is not True or not capability.get("evidence"):
                raise UserError(f"Video publishing is not verified for {account['label']}. Finish its capability setup.")
            data = specification.get("captions", {}).get(account["id"])
            if data is None:
                data = specification.get("captions", {}).get(account["platform"])
            if not isinstance(data, dict):
                raise UserError(f"Prepare a platform-specific caption for {account['label']}.")
            caption = validate_text(data.get("text"))
            platform_data = data.get("platform_data", {})
            if not isinstance(platform_data, dict):
                raise UserError("platform_data must be an object.")
            required = capability.get("required_fields", [])
            for key in required:
                if platform_data.get(key) in (None, ""):
                    raise UserError(f"{account['label']} needs platform field '{key}'.")
            if len(caption) > capability.get("caption_limit", 100000):
                raise UserError(f"Caption exceeds the configured limit for {account['label']}.")
            for measured, cap in (("bytes", "max_bytes"), ("duration", "max_duration")):
                if capability.get(cap) and target_qc[measured] > capability[cap]:
                    raise UserError(f"Media exceeds {cap} for {account['label']}; prepare an appropriate rendition.")
            due = data.get("scheduled_at") or specification.get("scheduled_at") or now()
            if mode == "schedule" and timestamp(due) <= datetime.now(timezone.utc):
                raise UserError("Scheduled time must be in the future; the provider publishes past times immediately.")
            if mode == "schedule":
                self.validate_schedule(account["id"],due,asset["id"])
            payload["targets"][account["id"]] = {
                "platform": account["platform"], "account_id": account["provider_id"],
                "profile_id": account["profile_id"], "text": caption, "platform_data": platform_data,
                "media": str(target_media), "media_hash": target_qc["sha256"],
                "due": timestamp(due).isoformat(), "proof": capability.get("proof", "public_url"),
            }
        request_id, payload_hash = str(uuid.uuid4()), digest(payload)
        with self.store.transaction():
            self.store.db.execute("INSERT INTO requests(id,asset_id,mode,payload,payload_hash,created) VALUES(?,?,?,?,?,?)",
                                  (request_id, asset["id"], mode, canonical(payload), payload_hash, now()))
            self.store.event("request-prepared", request_id, {"payload_hash": payload_hash, "targets": list(payload["targets"])})
        for destination_id, data in payload["targets"].items():
            self.captions.add(data["text"], "suggestion", asset_id=asset["id"], destination_id=destination_id,
                              note=f"Prepared request {request_id}; not a user-approved voice example.")
        return {"request_id": request_id, "payload_hash": payload_hash, "preview": payload}

    def authorize(self, request_id: str, payload_hash: str, note: str) -> dict:
        self.config = self.workspace.config
        if self.store.meta("restore_reconciliation_required") == "1":
            raise UserError("Restored workspace is quarantined; review provider history for every account, then run restore-reconcile.")
        request = self.store.one("SELECT * FROM requests WHERE id=?", (request_id,))
        if not request or request["payload_hash"] != payload_hash or digest(json.loads(request["payload"])) != payload_hash:
            raise UserError("Request changed or is missing; review its current preview.")
        if not isinstance(note, str) or not note.strip():
            raise UserError("Record the user's explicit request or applicable standing authorization.")
        if self.config["paused"]:
            raise UserError("Workspace is paused. Resume before authorizing publication.")
        payload = json.loads(request["payload"])
        results = []
        with self.store.transaction():
            for destination_id, target in payload["targets"].items():
                destination = self.store.one("SELECT * FROM destinations WHERE id=?", (destination_id,))
                if not destination or not destination["enabled"] or destination["profile_id"] not in self.config["profiles"] or destination["profile_id"] != target["profile_id"] or destination["provider_id"] != target["account_id"]:
                    raise UserError("Account scope/identity changed; prepare a new request.")
                existing = self.store.one("SELECT * FROM jobs WHERE asset_id=? AND destination_id=?", (request["asset_id"], destination_id))
                if request["mode"] == "schedule":
                    if timestamp(target["due"]) <= datetime.now(timezone.utc):
                        raise UserError("This preview's scheduled time has passed; prepare a new one.")
                    self.validate_schedule(destination_id,target["due"],request["asset_id"])
                if existing:
                    if existing["lease_until"] and existing["lease_until"] > time.time():
                        raise UserError("This job is currently being processed; wait for its outcome before revising it.")
                    # A duplicate message or routine tick cannot overwrite an existing request.
                    if existing["request_id"] != request_id:
                        unsubmitted=(existing["state"] == "prepared" and not existing["submitted"] and not existing["provider_id"]) or existing["state"]=="cancelled"
                        definitively_rejected=(existing["state"] == "failed" and existing["submitted"] and not existing["provider_id"]
                            and bool(re.search(r"Zernio returned HTTP (400|401|403|422)\.",existing["error"] or "")))
                        if unsubmitted or definitively_rejected:
                            # A validation/preflight-blocked job has no external side effect.
                            # Replace it only after this new immutable request is authorized.
                            self.store.event("prior-cycle-preserved",existing["id"],{"job":existing})
                            self.store.set_meta("cancel:"+existing["id"],None)
                            self.store.db.execute("UPDATE jobs SET request_id=?,due=?,payload=?,idempotency_key=?,state='prepared',provider_id=NULL,submitted=NULL,attempts=0,error=NULL,receipt=NULL,updated=? WHERE id=?",
                                (request_id,target["due"],canonical(target),str(uuid.uuid4()),now(),existing["id"]))
                            self.store.event("unsubmitted-job-revised",existing["id"],{"old_request":existing["request_id"],"new_request":request_id})
                            results.append({"destination_id":destination_id,"job_id":existing["id"],"state":"prepared","revised":True})
                            continue
                        results.append({"destination_id": destination_id, "job_id": existing["id"], "state": existing["state"], "reused": True,
                                        "note": "A prior publication cycle exists; use recover for a known failed post/draft or cancel a scheduled cycle before preparing a replacement."})
                        continue
                    results.append({"destination_id": destination_id, "job_id": existing["id"], "state": existing["state"], "reused": True})
                    continue
                if request["mode"] == "schedule" and timestamp(target["due"]) <= datetime.now(timezone.utc):
                    raise UserError("This preview's scheduled time has passed; prepare a new one.")
                if request["mode"] == "schedule":
                    self.validate_schedule(destination_id,target["due"],request["asset_id"])
                job_id = str(uuid.uuid4())
                self.store.add_obligations(request["asset_id"], [destination_id])
                self.store.db.execute("INSERT INTO jobs(id,request_id,asset_id,destination_id,due,payload,idempotency_key,updated) VALUES(?,?,?,?,?,?,?,?)",
                                      (job_id, request_id, request["asset_id"], destination_id, target["due"], canonical(target), str(uuid.uuid4()), now()))
                results.append({"destination_id": destination_id, "job_id": job_id, "state": "prepared", "reused": False})
            self.store.db.execute("UPDATE requests SET authorization=?,status='authorized' WHERE id=?", (note, request_id))
            self.store.event("request-authorized", request_id, {"payload_hash": payload_hash, "note": note})
        self.export_logs()
        return {"request_id": request_id, "jobs": results}

    def _claim(self, job_id: str, owner: str):
        with self.store.transaction():
            job = self.store.one("SELECT * FROM jobs WHERE id=?", (job_id,))
            if not job or job["state"] in {"verified", "failed", "cancelled", "provider_draft"}:
                return None
            if job["lease_until"] and job["lease_until"] > time.time():
                return None
            self.store.db.execute("UPDATE jobs SET lease_owner=?,lease_until=?,updated=? WHERE id=?",
                                  (owner, time.time()+600, now(), job_id))
            return job

    def _update(self, job_id, owner, **fields):
        allowed = {"state", "provider_id", "payload", "submitted", "attempts", "error", "receipt"}
        if set(fields)-allowed:
            raise RuntimeError("Unknown job fields")
        with self.store.transaction():
            assignments = ",".join(f"{key}=?" for key in fields)
            cursor = self.store.db.execute(f"UPDATE jobs SET {assignments},updated=? WHERE id=? AND lease_owner=? AND lease_until>?",
                                           (*fields.values(), now(), job_id, owner, time.time()))
            if cursor.rowcount != 1:
                raise UserError("Job ownership expired; reconcile before continuing.")
            if "payload" in fields:
                api=json.loads(fields["payload"]).get("api_payload")
                if api is not None: self.store.set_meta("api_payload_hash:"+job_id,digest(api))

    def _record_result(self, job: dict, owner: str, response: dict):
        post = extract_post(response)
        expected = json.loads(job["payload"])
        if job.get("provider_id") and (post.get("_id") or post.get("id")) != job["provider_id"]:
            raise UserError("Provider receipt belongs to a different post; preserve the attempt and review.")
        if not isinstance(post.get("platforms",[]),list) or any(not isinstance(v,dict) for v in post.get("platforms",[])):
            raise UserError("Provider receipt has malformed account evidence.")
        matching = [entry for entry in post.get("platforms", []) if entry.get("platform") == expected["platform"]
                    and (entry.get("accountId", {}).get("_id") if isinstance(entry.get("accountId"), dict) else entry.get("accountId")) == expected["account_id"]]
        state, error = "verification_pending", None
        entry = matching[0] if len(matching) == 1 else {}
        if len(matching)==1 and (entry.get("status") == "failed" or post.get("status") == "failed"):
            state, error = "failed", "Provider reports publication failure. Review before retrying."
        elif len(matching)==1 and post.get("status") == "draft":
            state = "provider_draft"
        elif len(matching)==1 and post.get("status") == "scheduled":
            state = "scheduled"
        elif entry.get("status") == "published":
            actual = entry.get("customContent") if entry.get("customContent") is not None else post.get("content")
            url = entry.get("platformPostUrl")
            identifier = entry.get("platformPostId") or entry.get("postId")
            if actual != expected["text"]:
                error = "Published caption differs from the approved text; review, do not recreate."
            elif expected["proof"] == "public_url" and (not url or not identifier):
                error = "Waiting for final platform identifier and public URL."
            elif not identifier:
                error = "Waiting for a final platform identifier."
            else:
                if url:
                    https_url(url)
                state = "verified"
        with self.store.transaction():
            cursor=self.store.db.execute("UPDATE jobs SET state=?,receipt=?,error=?,updated=? WHERE id=? AND lease_owner=? AND lease_until>?",(state,canonical(response),error,now(),job["id"],owner,time.time()))
            if cursor.rowcount != 1: raise UserError("Job ownership expired; reconcile before continuing.")
            self.store.event("provider-result", job["id"], {"state": state, "response": response})
            if state == "verified":
                self.store.db.execute("UPDATE obligations SET status='verified',updated=? WHERE asset_id=? AND destination_id=?",
                                      (now(), job["asset_id"], job["destination_id"]))
        return {"job_id": job["id"], "state": state, "error": error}

    def execute(self, job_id: str) -> dict:
        self.config = self.workspace.config
        if self.store.meta("restore_reconciliation_required") == "1":
            return {"job_id": job_id, "state": "restore_reconciliation_required"}
        pending=self.store.one("SELECT provider_id,submitted FROM jobs WHERE id=?",(job_id,))
        if self.config["paused"] and not (pending and pending["provider_id"]):
            return {"job_id": job_id, "state": "paused"}
        if not self.provider:
            raise UserError("Publishing provider is not connected.")
        if self.config["schedule"]["enabled"] and not self.config["approval"]["recurring_authorized"]:
            raise UserError("Recurring publication has no recorded approval; pause and complete onboarding first.")
        owner = str(uuid.uuid4())
        job = self._claim(job_id, owner)
        if job is None:
            return {"job_id": job_id, "state": "not_claimed"}
        submitted = False
        try:
            if job["state"]=="cancel_pending" or self.store.meta("cancel:"+job_id)=="1":
                return {"job_id":job_id,"state":"cancel_pending","next":"retry cancel; publication is blocked"}
            request = self.store.one("SELECT * FROM requests WHERE id=?", (job["request_id"],))
            if not request or not request["authorization"] or digest(json.loads(request["payload"])) != request["payload_hash"]:
                raise UserError("Missing or invalid request authorization.")
            target = json.loads(job["payload"])
            destination = self.store.one("SELECT * FROM destinations WHERE id=?", (job["destination_id"],))
            approved=json.loads(request["payload"])
            authorized_target=approved.get("targets",{}).get(job["destination_id"])
            immutable_target={key:value for key,value in target.items() if key!="api_payload"}
            if authorized_target != immutable_target or job["asset_id"] != request["asset_id"] or job["due"]!=target["due"]:
                raise UserError("Job differs from its authorized request; preserve and review it.")
            if job["provider_id"]:
                response = self.provider.get(job["provider_id"])
                return self._record_result(job, owner, response)
            if target.get("api_payload"):
                body=target["api_payload"]
                if not isinstance(body,dict): raise UserError("Cached provider payload must be an object.")
                required={"content":target["text"],"platforms":[{"platform":target["platform"],"accountId":target["account_id"],"platformSpecificData":target["platform_data"]}],"metadata":{"aiVerseJob":job["id"],"asset":job["asset_id"]}}
                if any(body.get(k)!=v for k,v in required.items()) or set(body)-{"content","platforms","metadata","mediaItems","publishNow","scheduledFor","isDraft"}:
                    raise UserError("Cached provider payload differs from the approved target.")
                timing={"now":{"publishNow":True},"schedule":{"scheduledFor":target["due"]},"draft":{"isDraft":True}}[request["mode"]]
                if {k:body[k] for k in ("publishNow","scheduledFor","isDraft") if k in body}!=timing:
                    raise UserError("Cached provider timing differs from the authorized request.")
                items=body.get("mediaItems")
                if not isinstance(items,list) or len(items)!=1 or items[0].get("type")!="video": raise UserError("Cached media payload is invalid.")
                https_url(items[0].get("url"))
                if self.store.meta("api_payload_hash:"+job_id)!=digest(body): raise UserError("Cached provider payload checksum changed; reconcile before sending.")
            if not destination or not destination["enabled"] or destination["profile_id"] not in self.config["profiles"] or not destination["connected"] or destination["provider_id"] != target["account_id"]:
                raise UserError("Account is disconnected or changed; preserve the request and reconnect.")
            if job["submitted"] and not job["provider_id"] and datetime.now(timezone.utc)-timestamp(job["submitted"]) >= timedelta(hours=23):
                raise UserError("Uncertain attempt is outside the safe idempotency window. Reconcile manually before retrying.")
            asset_payload = json.loads(request["payload"])
            media = Path(target.get("media",asset_payload["media"]))
            if file_hash(media) != target.get("media_hash",asset_payload["media_hash"]):
                raise UserError("Approved media changed; prepare and approve its new revision.")
            health = self.provider.health(target["account_id"])
            permissions=health.get("permissions") or {}
            if health.get("status") not in {"healthy", "ok"} or permissions.get("canPost") is not True or permissions.get("missingRequired"):
                raise UserError("Account publishing health/permissions are not explicitly ready; inspect provider status before retrying.")
            if "api_payload" not in target:
                media_url = https_url(self.provider.upload(media))
                if file_hash(media)!=target.get("media_hash",asset_payload["media_hash"]): raise UserError("Approved media changed during upload; publication blocked.")
                body = {"content": target["text"], "mediaItems": [{"type": "video", "url": media_url}],
                        "platforms": [{"platform": target["platform"], "accountId": target["account_id"], "platformSpecificData": target["platform_data"]}],
                        "metadata": {"aiVerseJob": job["id"], "asset": job["asset_id"]}}
                if request["mode"] == "now":
                    body["publishNow"] = True
                elif request["mode"] == "schedule":
                    if timestamp(target["due"]) <= datetime.now(timezone.utc):
                        raise UserError("Scheduled time passed before submission; choose a future time.")
                    body["scheduledFor"] = target["due"]
                else:
                    body["isDraft"] = True
                platform_data = body["platforms"][0]["platformSpecificData"]
                if target["platform"] == "youtube" and len(platform_data.get("title", "")) > 100:
                    raise UserError("YouTube's explicit title must be 100 characters or fewer.")
                if target["platform"] == "tiktok":
                    settings = platform_data.get("tiktokSettings") or {}
                    if settings.get("content_preview_confirmed") is not True or settings.get("express_consent_given") is not True or not settings.get("privacy_level"):
                        raise UserError("TikTok needs creator privacy, content preview confirmation, and express consent in platform_data.")
                self.provider.validate(body)
                target["api_payload"] = body
                self._update(job_id, owner, payload=canonical(target))
                job["payload"] = canonical(target)
            elif request["mode"] == "schedule" and not job["submitted"] and timestamp(target["due"]) <= datetime.now(timezone.utc):
                raise UserError("Scheduled time passed before submission; choose a future time.")
            self._update(job_id, owner, state="submitting", submitted=job["submitted"] or now(), attempts=job["attempts"]+1, error=None)
            submitted = True
            response = self.provider.create(target["api_payload"], job["idempotency_key"])
            post = extract_post(response)
            post_id = post.get("_id") or post.get("id")
            if not post_id:
                raise ProviderError("No provider identifier; reconcile this intent.", uncertain=True)
            self._update(job_id, owner, provider_id=post_id, state="verification_pending")
            job["provider_id"] = post_id
            # Read the stored record rather than trusting create acceptance as success.
            return self._record_result(job, owner, self.provider.get(post_id))
        except (UserError, OSError) as exc:
            state = "outcome_unknown" if submitted else job["state"]
            if isinstance(exc, ProviderError) and submitted and exc.status in {400, 401, 403, 422} and not exc.uncertain:
                state = "failed"
            self._update(job_id, owner, state=state, error=str(exc))
            self.store.event("job-error", job_id, {"state": state, "message": str(exc)})
            return {"job_id": job_id, "state": state, "error": str(exc)}
        finally:
            with self.store.transaction():
                self.store.db.execute("UPDATE jobs SET lease_owner=NULL,lease_until=NULL WHERE id=? AND lease_owner=?", (job_id, owner))
            self.close_asset(job["asset_id"])
            self.export_logs()

    def cancel_job(self, job_id: str) -> dict:
        """Cancel only unsent local work or a provider-confirmed scheduled post."""
        owner=str(uuid.uuid4())
        local_cancelled=False
        with self.store.transaction():
            job=self.store.one("SELECT * FROM jobs WHERE id=?",(job_id,))
            if not job:
                raise UserError("Unknown publication job.")
            if job["state"] == "cancelled":
                return {"job_id":job_id,"state":"cancelled","already_cancelled":True}
            if job["lease_until"] and job["lease_until"] > time.time():
                raise UserError("This job is being processed; reconcile its current provider state before cancelling.")
            if job["provider_id"] or job["submitted"]:
                if not job["provider_id"]:
                    raise UserError("This attempt has an unknown provider outcome; reconcile it before cancelling.")
                self.store.db.execute("UPDATE jobs SET lease_owner=?,lease_until=?,updated=? WHERE id=?",
                                      (owner,time.time()+600,now(),job_id))
            elif job["state"] == "prepared":
                self.store.db.execute("UPDATE jobs SET state='cancelled',error=NULL,updated=? WHERE id=?",(now(),job_id))
                self.store.event("job-cancelled",job_id,{"kind":"local-unsent"})
                local_cancelled=True
            else:
                raise UserError("Only unsent prepared work or confirmed provider-scheduled posts can be cancelled.")
        if local_cancelled:
            self.export_logs()
            return {"job_id":job_id,"state":"cancelled","external_action":False}
        try:
            if not self.provider:
                raise UserError("Connect Zernio to verify and cancel this scheduled post.")
            pending_cancel=self.store.meta("cancel:"+job_id)=="1"
            try:
                existing=extract_post(self.provider.get(job["provider_id"]))
            except ProviderError as exc:
                if pending_cancel and exc.status==404 and not exc.uncertain:
                    existing={"_id":job["provider_id"],"status":"cancelled","evidence":"authenticated provider absence after durable cancellation intent"}
                else: raise
            status=existing.get("status")
            if status in {"cancelled","canceled"}:
                verified=existing
            elif status == "scheduled":
                with self.store.transaction():
                    self.store.set_meta("cancel:"+job_id,"1")
                    self.store.event("cancellation-intent",job_id,{"provider_id":job["provider_id"]})
                deleted=self.provider.cancel(job["provider_id"])
                try:
                    verified=extract_post(self.provider.get(job["provider_id"]))
                except ProviderError as exc:
                    if exc.status==404 and not exc.uncertain:
                        verified={"_id":job["provider_id"],"status":"cancelled","delete_acknowledgment":deleted,"evidence":"authenticated absence after deletion"}
                    else: raise
            else:
                if status in {"published","partial","failed"}:
                    reconciled=self._record_result(job,owner,{"post":existing})
                    return {"job_id":job_id,"state":"not_cancellable","provider_status":status,
                            "reconciled_state":reconciled["state"],"external_action":False}
                return {"job_id":job_id,"state":"not_cancellable","provider_status":status or "unknown",
                        "external_action":False}
            if verified.get("status") not in {"cancelled","canceled"}:
                raise UserError("Provider did not confirm cancellation; the post may still be scheduled.")
            with self.store.transaction():
                self.store.db.execute("UPDATE jobs SET state='cancelled',receipt=?,error=NULL,updated=? WHERE id=? AND lease_owner=?",
                    (canonical(verified),now(),job_id,owner))
                self.store.event("job-cancelled",job_id,{"kind":"provider-scheduled","provider_id":job["provider_id"]})
            self.export_logs()
            return {"job_id":job_id,"state":"cancelled","external_action":True}
        except UserError as exc:
            with self.store.transaction():
                self.store.db.execute("UPDATE jobs SET state='cancel_pending',error=?,updated=? WHERE id=? AND lease_owner=?",(str(exc),now(),job_id,owner))
                self.store.event("job-cancel-pending",job_id,{"message":str(exc)})
            return {"job_id":job_id,"state":"cancel_pending","error":str(exc)}
        finally:
            with self.store.transaction():
                self.store.db.execute("UPDATE jobs SET lease_owner=NULL,lease_until=NULL WHERE id=? AND lease_owner=?",(job_id,owner))
            self.export_logs()

    def recover(self, job_id, operation, note, *, provider_id=None, request_id=None, payload_hash=None):
        """Explicit recovery of a known operation, without fresh provider creation."""
        self.config=self.workspace.config
        if not isinstance(note,str) or not note.strip(): raise UserError("Record explicit recovery authorization/evidence.")
        if self.store.meta("restore_reconciliation_required")=="1": raise UserError("Reconcile restore quarantine first.")
        if not self.provider: raise UserError("Connect Zernio for recovery.")
        owner=str(uuid.uuid4())
        with self.store.transaction():
            job=self.store.one("SELECT * FROM jobs WHERE id=?",(job_id,))
            if not job: raise UserError("Unknown publication job.")
            if job["lease_until"] and job["lease_until"]>time.time(): raise UserError("Job is being processed; wait for its result.")
            self.store.db.execute("UPDATE jobs SET lease_owner=?,lease_until=?,updated=? WHERE id=?",(owner,time.time()+600,now(),job_id))
        try:
            approved_request=self.store.one("SELECT * FROM requests WHERE id=?",(job["request_id"],))
            current=json.loads(job["payload"])
            if not approved_request or not approved_request["authorization"] or digest(json.loads(approved_request["payload"]))!=approved_request["payload_hash"] or json.loads(approved_request["payload"]).get("targets",{}).get(job["destination_id"])!={key:value for key,value in current.items() if key!="api_payload"}:
                raise UserError("Recovery job differs from its original immutable authorization.")
            identity=job["provider_id"] or provider_id
            if not identity: raise UserError("Recovery requires the existing provider post ID; never guess or recreate it.")
            response=self.provider.get(identity)
            post=extract_post(response)
            if (post.get("_id") or post.get("id"))!=identity: raise UserError("Provider returned a different record.")
            if not job["provider_id"]:
                metadata=post.get("metadata",{})
                if not isinstance(metadata,dict) or metadata.get("aiVerseJob")!=job_id or metadata.get("asset")!=job["asset_id"]:
                    raise UserError("Provider metadata does not prove this unknown attempt belongs to the saved job.")
                self._update(job_id,owner,provider_id=identity)
                job["provider_id"]=identity
            target=json.loads(job["payload"])
            if not isinstance(post.get("platforms",[]),list) or any(not isinstance(v,dict) for v in post.get("platforms",[])): raise UserError("Provider destination evidence is malformed.")
            matched=[entry for entry in post.get("platforms",[]) if entry.get("platform")==target["platform"] and (entry.get("accountId",{}).get("_id") if isinstance(entry.get("accountId"),dict) else entry.get("accountId"))==target["account_id"]]
            if len(matched)!=1: raise UserError("Provider record does not prove the exact destination account.")
            pending=self.store.meta("recovery:"+job_id)
            if operation=="reconcile" or pending:
                result=self._record_result(job,owner,response)
                if pending: result["recovery_intent"]=pending; result["note"]="Reconciled only; uncertain recovery is never automatically resent."
                return result
            if self.config["paused"]: raise UserError("Workspace paused; reconciliation is allowed but sending is paused.")
            destination=self.store.one("SELECT * FROM destinations WHERE id=?",(job["destination_id"],))
            if not destination["enabled"] or not destination["connected"] or destination["profile_id"] not in self.config["profiles"] or destination["provider_id"]!=target["account_id"]:
                raise UserError("Account scope changed; recovery cannot send.")
            health=self.provider.health(target["account_id"])
            permissions=health.get("permissions") or {}
            if health.get("status") not in {"healthy","ok"} or permissions.get("canPost") is not True or permissions.get("missingRequired"):
                raise UserError("Recovery publishing permissions are not ready.")
            media=Path(target.get("media",json.loads(approved_request["payload"])["media"]))
            if file_hash(media)!=target.get("media_hash",json.loads(approved_request["payload"])["media_hash"]): raise UserError("Approved media changed; recovery cannot send.")
            if operation=="retry-failed":
                if post.get("status")!="failed" or matched[0].get("status")!="failed": raise UserError("Only a confirmed failed existing post can be retried.")
                actual=matched[0].get("customContent",post.get("content"))
                if actual!=target["text"]: raise UserError("Failed post text differs from its approved request.")
            elif operation=="promote-draft":
                if post.get("status")!="draft": raise UserError("Only an existing provider draft can be promoted.")
                request=self.store.one("SELECT * FROM requests WHERE id=?",(request_id,))
                if not request or request["payload_hash"]!=payload_hash or digest(json.loads(request["payload"]))!=payload_hash or request["asset_id"]!=job["asset_id"] or request["mode"] not in {"now","schedule"}:
                    raise UserError("Draft promotion needs a new reviewed publication preview and hash.")
                approved=json.loads(request["payload"])
                new=approved["targets"].get(job["destination_id"])
                if not new or new["account_id"]!=target["account_id"] or new.get("media_hash")!=target.get("media_hash"):
                    raise UserError("Draft promotion must keep the same account and media revision.")
                body=dict(target.get("api_payload",{}))
                if not body: raise UserError("Draft has no saved provider payload.")
                for key in ("isDraft","publishNow","scheduledFor"): body.pop(key,None)
                body.update(content=new["text"],platforms=[{"platform":new["platform"],"accountId":new["account_id"],"platformSpecificData":new["platform_data"]}],isDraft=False)
                if request["mode"]=="now": body["publishNow"]=True
                else:
                    if timestamp(new["due"])<=datetime.now(timezone.utc): raise UserError("Promotion time has passed.")
                    self.validate_schedule(job["destination_id"],new["due"],job["asset_id"])
                    body["scheduledFor"]=new["due"]
                self.provider.validate(body)
                new=dict(new,api_payload=body)
                with self.store.transaction():
                    self.store.db.execute("UPDATE requests SET status='authorized',authorization=? WHERE id=?",(note,request_id))
                    self.store.db.execute("UPDATE jobs SET request_id=?,payload=?,due=? WHERE id=? AND lease_owner=?",(request_id,canonical(new),new["due"],job_id,owner))
                    self.store.set_meta("api_payload_hash:"+job_id,digest(body))
                    self.store.event("draft-promotion-approved",job_id,{"request_id":request_id,"payload_hash":payload_hash,"note":note})
                job["payload"]=canonical(new)
            else: raise UserError("Unknown recovery operation.")
            with self.store.transaction():
                self.store.set_meta("recovery:"+job_id,operation)
                self.store.event("recovery-intent",job_id,{"operation":operation,"provider_id":identity,"note":note})
            self._update(job_id,owner,state="verification_pending")
            if operation=="retry-failed": self.provider.retry(identity)
            else: self.provider.update(identity,body)
            return self._record_result(job,owner,self.provider.get(identity))
        except (UserError,OSError) as exc:
            self._update(job_id,owner,error=str(exc))
            return {"job_id":job_id,"state":"recovery_pending","error":str(exc)}
        finally:
            self.store.db.execute("UPDATE jobs SET lease_owner=NULL,lease_until=NULL WHERE id=? AND lease_owner=?",(job_id,owner))
            self.close_asset(job["asset_id"])
            self.export_logs()

    def close_asset(self, asset_id: str):
        coverage = self.store.coverage(asset_id)
        if not coverage["complete"]:
            return
        asset = self.store.require_asset(asset_id)
        # Posted library assets stay there while newly added destinations backfill them.
        category="library" if asset["library"] else "request-scoped"
        destination = self.workspace.path / "media" / "posted" / category / Path(asset["path"]).name
        with self.store.transaction():
            self.store.db.execute("INSERT INTO actions(id,asset_id,kind,data,state) VALUES(?,?,?,?,'pending') ON CONFLICT(asset_id,kind) DO UPDATE SET data=excluded.data,state='pending',error=NULL",
                                  (str(uuid.uuid4()), asset_id, "archive", canonical({"path": str(destination), "library": bool(asset["library"])})))
        try:
            # Keep originals; archive is an additional verified copy, not destructive cleanup.
            destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            if destination.exists():
                if file_hash(destination)!=asset["hash"]:
                    raise UserError("Archive copy differs from the verified source; preserve both and review.")
            else:
                with tempfile.NamedTemporaryFile(dir=destination.parent,delete=False) as handle:
                    temporary=Path(handle.name)
                    try:
                        with Path(asset["path"]).open("rb") as incoming: shutil.copyfileobj(incoming,handle)
                        handle.flush()
                        if file_hash(temporary)!=asset["hash"]: raise UserError("Archive verification failed; original preserved.")
                        os.replace(temporary,destination)
                    finally: temporary.unlink(missing_ok=True)
            if file_hash(destination) != asset["hash"]:
                raise UserError("Archive verification failed; original preserved.")
            with self.store.transaction():
                self.store.db.execute("UPDATE assets SET state='posted' WHERE id=?", (asset_id,))
                self.store.db.execute("UPDATE actions SET state='done',error=NULL WHERE asset_id=? AND kind='archive'", (asset_id,))
                self.store.event("archived", asset_id, {"library": bool(asset["library"]),"path":str(destination)})
        except (OSError,UserError) as exc:
            self.store.db.execute("UPDATE actions SET error=? WHERE asset_id=? AND kind='archive'", (str(exc),asset_id))

    def sync_drive(self, asset_id: str | None = None) -> dict:
        """Move only fully verified Drive originals to Posted; retry is sync-only."""
        self.config=self.workspace.config
        if not self.config["drive"]["enabled"]:
            raise UserError("Google Drive is not connected in this workspace.")
        folders=self.config["drive"]["folders"]
        if not folders.get("posted"):
            raise UserError("Map the customer's Posted folder during onboarding.")
        # Backfill the original single-source fields before the source-link table was added.
        for asset in self.store.rows("SELECT id,drive_id,drive_parent FROM assets WHERE drive_id IS NOT NULL"):
            self.store.db.execute("INSERT OR IGNORE INTO asset_sources(asset_id,provider,source_id,parent,created) VALUES(?, 'gdrive', ?, ?, ?)",
                                  (asset["id"],asset["drive_id"],asset["drive_parent"],now()))
        query = "SELECT s.asset_id,s.source_id,s.parent,s.version FROM asset_sources s WHERE s.provider='gdrive'"
        params = ()
        if asset_id:
            query += " AND s.asset_id=?"
            params = (asset_id,)
        results = []
        eligible = []
        batch=self.config["limits"]["max_jobs_per_tick"]
        cursor_key="drive_sync_cursor:"+(asset_id or "all")
        cursor=self.store.meta(cursor_key,"")
        query+=" ORDER BY CASE WHEN s.source_id>? THEN 0 ELSE 1 END,s.source_id LIMIT ?"
        for row in self.store.rows(query, (*params,cursor,batch)):
            self.store.set_meta(cursor_key,row["source_id"])
            asset=self.store.require_asset(row["asset_id"])
            target_folder=folders.get("posted") if asset["library"] else folders.get("request_scoped")
            if not target_folder:
                results.append({"asset_id":row["asset_id"],"source_id":row["source_id"],"state":"needs_request_archive_folder"})
                continue
            coverage = self.store.coverage(row["asset_id"])
            if not coverage["complete"]:
                results.append({"asset_id": row["asset_id"],"source_id":row["source_id"], "state": "waiting_for_accounts"})
                continue
            if row["parent"] == target_folder:
                results.append({"asset_id": row["asset_id"],"source_id":row["source_id"], "state": "already_filed"})
                continue
            eligible.append((row,target_folder))
        local_exports=[]
        if self.config["drive"].get("archive_local_uploads",False):
            local_query="SELECT * FROM assets WHERE drive_id IS NULL AND NOT EXISTS (SELECT 1 FROM asset_sources s WHERE s.asset_id=assets.id AND s.provider='gdrive')"
            local_params=()
            if asset_id: local_query+=" AND id=?"; local_params=(asset_id,)
            export_cursor_key="drive_export_cursor:"+(asset_id or "all")
            export_cursor=self.store.meta(export_cursor_key,"")
            local_query+=" ORDER BY CASE WHEN id>? THEN 0 ELSE 1 END,id LIMIT ?"
            for asset in self.store.rows(local_query,(*local_params,export_cursor,batch)):
                self.store.set_meta(export_cursor_key,asset["id"])
                if self.store.one("SELECT 1 FROM asset_sources WHERE asset_id=? AND provider='gdrive' LIMIT 1",(asset["id"],)):
                    continue
                target_folder=folders.get("posted") if asset["library"] else folders.get("request_scoped")
                if not target_folder:
                    results.append({"asset_id":asset["id"],"state":"needs_request_archive_folder"})
                    continue
                if not self.store.coverage(asset["id"])["complete"]:
                    results.append({"asset_id":asset["id"],"state":"waiting_for_accounts"})
                    continue
                local_exports.append((asset,target_folder))
        if not eligible and not local_exports:
            return {"results": results}
        from .drive import Drive
        drive = Drive(self.config["drive"])
        for row,target_folder in eligible:
            current_config=self.workspace.config
            current_folders=current_config["drive"]["folders"]
            expected_target=current_folders.get("posted") if self.store.require_asset(row["asset_id"])["library"] else current_folders.get("request_scoped")
            if not current_config["drive"]["enabled"] or expected_target != target_folder:
                results.append({"asset_id":row["asset_id"],"source_id":row["source_id"],"state":"scope_changed_before_move"})
                continue
            try:
                observed=drive.metadata(row["source_id"])
                if not isinstance(observed,dict) or observed.get("trashed"): raise UserError("Drive source is unavailable; preserve the record.")
                if not row["version"]:
                    original=self.store.require_asset(row["asset_id"])
                    if not observed.get("md5Checksum") or file_hash(Path(original["path"]),"md5")!=observed["md5Checksum"]:
                        raise UserError("Legacy Drive source lacks matching revision/checksum proof; ingest it again before filing.")
                if row["version"]:
                    if str(observed.get("version"))!=str(row["version"]) or observed.get("trashed"):
                        raise UserError("Drive source revision changed since ingestion; ingest the new revision before filing.")
                moved=drive.move(row["source_id"], row["parent"], target_folder)
            except UserError as exc:
                self.store.event("drive-sync-error", row["asset_id"], {"source_id":row["source_id"],"message": str(exc)})
                results.append({"asset_id": row["asset_id"],"source_id":row["source_id"], "state": "sync_pending", "error": str(exc)})
                continue
            actual_parents=moved.get("parents",[]) if isinstance(moved,dict) else []
            if target_folder not in actual_parents:
                results.append({"asset_id":row["asset_id"],"source_id":row["source_id"],"state":"sync_pending",
                                "error":"Drive move response did not confirm the target folder."})
                continue
            # Record the observed remote parent immediately. If settings change or
            # become unreadable now, the next sync still has the actual source parent.
            with self.store.transaction():
                self.store.db.execute("UPDATE asset_sources SET parent=?,version=COALESCE(?,version) WHERE provider='gdrive' AND source_id=?", (target_folder,moved.get("version"),row["source_id"]))
                self.store.db.execute("UPDATE assets SET drive_parent=? WHERE id=? AND drive_id=?", (target_folder,row["asset_id"],row["source_id"]))
                self.store.event("drive-move-observed",row["asset_id"],{"drive_id":row["source_id"],"actual_folder":target_folder})
            latest_config=self.workspace.config
            latest_folders=latest_config["drive"]["folders"]
            latest_target=latest_folders.get("posted") if self.store.require_asset(row["asset_id"])["library"] else latest_folders.get("request_scoped")
            if not latest_config["drive"]["enabled"] or latest_target != target_folder:
                with self.store.transaction():
                    self.store.event("drive-scope-changed-during-move",row["asset_id"],{"drive_id":row["source_id"],"actual_folder":target_folder,"current_target":latest_target})
                results.append({"asset_id":row["asset_id"],"source_id":row["source_id"],"state":"scope_changed_after_move",
                                "actual_folder":target_folder,"next_target":latest_target})
                continue
            with self.store.transaction():
                self.store.db.execute("UPDATE assets SET state='posted' WHERE id=? AND drive_id=?", (row["asset_id"],row["source_id"]))
                self.store.event("drive-filed", row["asset_id"], {"drive_id": row["source_id"], "folder_id": target_folder,
                                    "category":"library" if self.store.require_asset(row["asset_id"])["library"] else "request-scoped"})
            results.append({"asset_id": row["asset_id"],"source_id":row["source_id"], "state": "filed"})
        for asset,target_folder in local_exports:
            current_config=self.workspace.config
            current_folders=current_config["drive"]["folders"]
            current_target=current_folders.get("posted") if asset["library"] else current_folders.get("request_scoped")
            if not current_config["drive"].get("enabled") or not current_config["drive"].get("archive_local_uploads",False) or current_target!=target_folder:
                results.append({"asset_id":asset["id"],"state":"scope_changed_before_upload"})
                continue
            export=self.store.one("SELECT * FROM drive_exports WHERE asset_id=?",(asset["id"],))
            try:
                if export and export["sha256"]!=asset["hash"]:
                    raise UserError("Saved Drive export belongs to a different video revision; review before uploading.")
                if export is None:
                    candidate_id=drive.generate_file_id()
                    with self.store.transaction():
                        export=self.store.one("SELECT * FROM drive_exports WHERE asset_id=?",(asset["id"],))
                        if export is None:
                            self.store.db.execute("INSERT INTO drive_exports(asset_id,file_id,target_folder,sha256,size,state,updated) VALUES(?,?,?,?,?,'reserved',?)",
                                (asset["id"],candidate_id,target_folder,asset["hash"],Path(asset["path"]).stat().st_size,now()))
                            export=self.store.one("SELECT * FROM drive_exports WHERE asset_id=?",(asset["id"],))
                with self.store.transaction():
                    self.store.db.execute("UPDATE drive_exports SET target_folder=?,error=NULL,updated=? WHERE asset_id=?",
                        (target_folder,now(),asset["id"]))
                uploaded=drive.ensure_uploaded(Path(asset["path"]),target_folder,export["file_id"],asset["id"],asset["hash"])
                actual_parents=uploaded.get("parents",[])
                if target_folder not in actual_parents:
                    raise UserError("Drive did not confirm the configured archive folder; retry filing only.")
                with self.store.transaction():
                    self.store.db.execute("INSERT INTO asset_sources(asset_id,provider,source_id,parent,version,created) VALUES(?, 'gdrive', ?, ?, ?, ?) ON CONFLICT(provider,source_id) DO UPDATE SET asset_id=excluded.asset_id,parent=excluded.parent,version=excluded.version",
                        (asset["id"],export["file_id"],target_folder,uploaded.get("version"),now()))
                    self.store.db.execute("UPDATE drive_exports SET state='filed',error=NULL,updated=? WHERE asset_id=?",(now(),asset["id"]))
                    self.store.event("drive-archive-uploaded",asset["id"],{"file_id":export["file_id"],"folder_id":target_folder})
                latest=self.workspace.config
                latest_folders=latest["drive"]["folders"]
                latest_target=latest_folders.get("posted") if asset["library"] else latest_folders.get("request_scoped")
                if not latest["drive"].get("enabled") or latest_target!=target_folder:
                    results.append({"asset_id":asset["id"],"state":"scope_changed_after_upload","file_id":export["file_id"],"actual_folder":target_folder,"next_target":latest_target})
                    continue
                results.append({"asset_id":asset["id"],"state":"uploaded_and_filed","file_id":export["file_id"],"folder_id":target_folder})
            except (UserError,OSError) as exc:
                with self.store.transaction():
                    self.store.db.execute("UPDATE drive_exports SET state='pending',error=?,updated=? WHERE asset_id=?",(str(exc),now(),asset["id"]))
                    self.store.event("drive-archive-upload-pending",asset["id"],{"message":str(exc)})
                results.append({"asset_id":asset["id"],"state":"sync_pending","error":str(exc)})
        return {"results": results}

    def tick(self):
        self.config=self.workspace.config
        start, results = time.monotonic(), []
        priority=int(self.store.meta("tick_priority","0"))%2
        self.store.set_meta("tick_priority",str(1-priority))
        jobs = self.store.rows("SELECT id FROM jobs WHERE state IN ('prepared','submitting','scheduled','verification_pending','outcome_unknown','cancel_pending') AND COALESCE(lease_until,0)<=? AND (?=0 OR provider_id IS NOT NULL) ORDER BY CASE WHEN provider_id IS NULL AND submitted IS NULL THEN ? ELSE ? END,updated,due LIMIT ?",(time.time(),int(self.config["paused"]),priority,1-priority,self.config["limits"]["max_jobs_per_tick"]))
        for job in jobs:
            if len(results) >= self.config["limits"]["max_jobs_per_tick"] or time.monotonic()-start >= self.config["limits"]["tick_seconds"]:
                break
            if self.store.meta("cancel:"+job["id"])=="1": results.append(self.cancel_job(job["id"]))
            else: results.append(self.execute(job["id"]))
        archive_cursor=self.store.meta("archive_retry_cursor","")
        for action in self.store.rows("SELECT id,asset_id FROM actions WHERE kind='archive' AND state='pending' ORDER BY CASE WHEN id>? THEN 0 ELSE 1 END,id LIMIT ?",(archive_cursor,self.config["limits"]["max_jobs_per_tick"])):
            self.store.set_meta("archive_retry_cursor",action["id"])
            if time.monotonic()-start >= self.config["limits"]["tick_seconds"]: break
            self.close_asset(action["asset_id"])
        remaining=self.store.one("SELECT count(*) AS n FROM jobs WHERE state IN ('prepared','submitting','scheduled','verification_pending','outcome_unknown','cancel_pending')")["n"]
        return {"results": results, "remaining": remaining}

    def acknowledge_restore_reconciliation(self, note: str) -> dict:
        if self.store.meta("restore_reconciliation_required") != "1":
            return {"required": False, "state": "not_a_restored_workspace"}
        if not isinstance(note, str) or not note.strip() or len(note) > 5000:
            raise UserError("Record which provider accounts/history were reviewed before clearing restore quarantine.")
        destinations = self.store.rows("SELECT * FROM destinations")
        if destinations:
            if not self.provider:
                raise UserError("Connect Zernio and refresh the saved account inventory before reconciling the restore.")
            listed = self.provider.accounts(self.config["profiles"])
            live_ids = {row.get("_id") for row in listed if row.get("_id") and row.get("platform")}
            missing = [row["provider_id"] for row in destinations if row["provider_id"] not in live_ids]
            if missing:
                raise UserError("Some saved provider accounts are missing from the connected profile inventory; resolve them before resuming.")
            by_id={row["_id"]:row for row in listed}
            for destination in destinations:
                live=by_id[destination["provider_id"]]
                profile=live.get("profileId")
                profile=profile.get("_id") if isinstance(profile,dict) else profile
                native=live.get("platformUserId")
                native_key=f"{profile}:{live.get('platform')}:{native}" if native else None
                if profile!=destination["profile_id"] or live.get("platform")!=destination["platform"] or (destination["native_key"] and native_key!=destination["native_key"]):
                    raise UserError("Restored account native/profile identity does not match the live inventory; quarantine remains.")
        with self.store.transaction():
            self.store.db.execute("UPDATE meta SET value='0' WHERE key='restore_reconciliation_required'")
            self.store.event("restore-reconciled", "workspace", {"review_note": note.strip()})
        return {"required": False, "state": "reconciled", "paused": self.config["paused"]}

    def request_coverage(self, request_id):
        request=self.store.one("SELECT * FROM requests WHERE id=?",(request_id,))
        if not request: raise UserError("Unknown publication request.")
        payload=json.loads(request["payload"])
        result=[]
        for destination,target in payload["targets"].items():
            job=self.store.one("SELECT id,request_id,state,provider_id,error FROM jobs WHERE asset_id=? AND destination_id=?",(request["asset_id"],destination))
            result.append({"destination_id":destination,"platform":target["platform"],"due":target["due"],"job":job,"uses_prior_cycle":bool(job and job["request_id"]!=request_id)})
        return {"request_id":request_id,"status":request["status"],"targets":result,"published_complete":bool(result) and all(item["job"] and item["job"]["state"]=="verified" for item in result)}

    def export_logs(self):
        try:
            self._export_logs()
            self.store.set_meta("derived_export_error",None)
        except OSError as exc:
            self.store.set_meta("derived_export_error",f"Derived logs pending: {type(exc).__name__}; retry export.")

    def _export_logs(self):
        headers = ["job_id", "asset_id", "title", "state", "due", "provider_id", "error", "updated", "platform_post_id", "platform_post_url"]
        for destination in self.store.rows("SELECT * FROM destinations ORDER BY id"):
            rows = self.store.rows("""SELECT j.id AS job_id,j.asset_id,a.title,j.state,j.due,j.provider_id,j.error,j.updated,j.receipt,j.payload
                FROM jobs j JOIN assets a ON a.id=j.asset_id WHERE j.destination_id=? ORDER BY j.updated""", (destination["id"],))
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=headers)
            writer.writeheader()
            # Prevent spreadsheet formula execution when exports are opened in Excel/Sheets.
            for row in rows:
                raw=row.pop("receipt"); expected=json.loads(row.pop("payload"))
                row.update(platform_post_id="",platform_post_url="")
                if raw:
                    try:
                        post=extract_post(json.loads(raw))
                        for entry in post.get("platforms",[]):
                            identity=entry.get("accountId")
                            identity=identity.get("_id") if isinstance(identity,dict) else identity
                            if identity==expected["account_id"] and entry.get("platform")==expected["platform"]:
                                row["platform_post_id"]=entry.get("platformPostId") or entry.get("postId") or ""
                                row["platform_post_url"]=entry.get("platformPostUrl") or ""
                    except UserError: pass
                writer.writerow({key: "'"+str(value) if isinstance(value,str) and value.startswith(("=","+","-","@")) else value for key,value in row.items()})
            atomic_write(self.workspace.path / "logs" / "accounts" / f"{destination['platform']}-{destination['id']}.csv", output.getvalue())
        status = [self.store.coverage(row["id"]) for row in self.store.rows("SELECT id FROM assets")]
        atomic_write(self.workspace.path / "logs" / "video-coverage.json", json.dumps(status, ensure_ascii=False, indent=2))
