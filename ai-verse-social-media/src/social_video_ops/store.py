from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from .util import UserError, canonical, now
from .config import validate_capability

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS destinations (
 id TEXT PRIMARY KEY, provider_id TEXT NOT NULL UNIQUE, native_key TEXT,
 profile_id TEXT NOT NULL, platform TEXT NOT NULL, label TEXT NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 0, connected INTEGER NOT NULL DEFAULT 1,
 capability TEXT NOT NULL DEFAULT '{}', updated TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS native_identity ON destinations(native_key)
 WHERE native_key IS NOT NULL;
CREATE TABLE IF NOT EXISTS assets (
 id TEXT PRIMARY KEY, hash TEXT NOT NULL UNIQUE, title TEXT NOT NULL, path TEXT NOT NULL,
 drive_id TEXT, drive_parent TEXT, library INTEGER NOT NULL DEFAULT 0,
 state TEXT NOT NULL DEFAULT 'ready', transcript TEXT NOT NULL DEFAULT '',
 created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS obligations (
 asset_id TEXT NOT NULL REFERENCES assets(id), destination_id TEXT NOT NULL REFERENCES destinations(id),
 status TEXT NOT NULL DEFAULT 'pending', updated TEXT NOT NULL,
 PRIMARY KEY(asset_id,destination_id)
);
CREATE TABLE IF NOT EXISTS requests (
 id TEXT PRIMARY KEY, asset_id TEXT NOT NULL REFERENCES assets(id), mode TEXT NOT NULL,
 payload TEXT NOT NULL, payload_hash TEXT NOT NULL, authorization TEXT,
 created TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft'
);
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES requests(id),
 asset_id TEXT NOT NULL REFERENCES assets(id), destination_id TEXT NOT NULL REFERENCES destinations(id),
 state TEXT NOT NULL DEFAULT 'prepared', due TEXT NOT NULL, provider_id TEXT,
 payload TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
 submitted TEXT, lease_owner TEXT, lease_until REAL, attempts INTEGER NOT NULL DEFAULT 0,
 error TEXT, receipt TEXT, updated TEXT NOT NULL,
 UNIQUE(asset_id,destination_id)
);
CREATE TABLE IF NOT EXISTS events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, entity TEXT NOT NULL,
 data TEXT NOT NULL, created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS caption_entries (
 id TEXT PRIMARY KEY, asset_id TEXT, destination_id TEXT, kind TEXT NOT NULL,
 text TEXT NOT NULL, parent_id TEXT REFERENCES caption_entries(id),
 note TEXT NOT NULL DEFAULT '', created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actions (
 id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, kind TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', data TEXT NOT NULL, error TEXT,
 UNIQUE(asset_id,kind)
);
CREATE TABLE IF NOT EXISTS drive_intake (
 file_id TEXT PRIMARY KEY, name TEXT NOT NULL, version TEXT, size INTEGER, modified TEXT,
 state TEXT NOT NULL DEFAULT 'pending', error TEXT, asset_id TEXT, lease_owner TEXT, lease_until REAL,
 updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS asset_sources (
 asset_id TEXT NOT NULL REFERENCES assets(id), provider TEXT NOT NULL, source_id TEXT NOT NULL,
 parent TEXT, version TEXT, created TEXT NOT NULL,
 PRIMARY KEY(provider,source_id)
);
CREATE TABLE IF NOT EXISTS drive_exports (
 asset_id TEXT PRIMARY KEY REFERENCES assets(id), file_id TEXT NOT NULL UNIQUE,
 target_folder TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
 state TEXT NOT NULL DEFAULT 'reserved', error TEXT, updated TEXT NOT NULL
);
"""


REPEAT_SCHEMA = """
CREATE TABLE IF NOT EXISTS works (id TEXT PRIMARY KEY, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS work_members (
 asset_id TEXT PRIMARY KEY REFERENCES assets(id), work_id TEXT NOT NULL REFERENCES works(id));
CREATE INDEX IF NOT EXISTS work_members_work ON work_members(work_id);
CREATE TABLE IF NOT EXISTS repeat_fingerprints (
 asset_id TEXT PRIMARY KEY REFERENCES assets(id), source_hash TEXT NOT NULL,
 algorithm TEXT NOT NULL, data TEXT NOT NULL, status TEXT NOT NULL, error TEXT, updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS repeat_decisions (
 id TEXT PRIMARY KEY, asset_id TEXT NOT NULL REFERENCES assets(id), source_hash TEXT NOT NULL,
 matched_work TEXT NOT NULL, matched_hash TEXT NOT NULL, decision TEXT NOT NULL,
 policy_hash TEXT NOT NULL, note TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS work_destination_claims (
 work_id TEXT NOT NULL REFERENCES works(id), destination_id TEXT NOT NULL REFERENCES destinations(id),
 job_id TEXT NOT NULL REFERENCES jobs(id), updated TEXT NOT NULL,
 PRIMARY KEY(work_id,destination_id));
CREATE TABLE IF NOT EXISTS repeat_holds (
 asset_id TEXT PRIMARY KEY REFERENCES assets(id), status TEXT NOT NULL, data TEXT NOT NULL,
 updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS repeat_source_actions (
 source_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL REFERENCES assets(id), matched_work TEXT NOT NULL,
 original_parent TEXT NOT NULL, version TEXT, checksum TEXT, target_parent TEXT,
 state TEXT NOT NULL, error TEXT, updated TEXT NOT NULL);
"""


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        existing = self.db.execute("SELECT name FROM sqlite_master WHERE name='meta'").fetchone()
        version = self.db.execute("SELECT value FROM meta WHERE key='schema'").fetchone() if existing else None
        if version and version[0] not in {"1", "2"}:
            self.db.close()
            raise UserError("Database needs a supported migration before it can be opened.")
        # executescript commits implicitly: put BEGIN and the version update in
        # that same script so new tables, identity seeds and version commit together.
        try:
            self.db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + REPEAT_SCHEMA + """
                INSERT OR IGNORE INTO works SELECT id,created FROM assets;
                INSERT OR IGNORE INTO work_members SELECT id,id FROM assets;
                INSERT OR IGNORE INTO work_destination_claims
                  SELECT m.work_id,j.destination_id,j.id,j.updated FROM jobs j
                  JOIN work_members m ON m.asset_id=j.asset_id
                  WHERE COALESCE((SELECT value FROM meta WHERE key='schema'),'1')!='2' AND j.state!='cancelled' AND NOT (j.state='failed' AND j.provider_id IS NULL
                    AND (j.submitted IS NULL OR j.error GLOB '*HTTP 400.*' OR j.error GLOB '*HTTP 401.*'
                      OR j.error GLOB '*HTTP 403.*' OR j.error GLOB '*HTTP 422.*'));
                INSERT INTO meta(key,value) VALUES('schema','2')
                  ON CONFLICT(key) DO UPDATE SET value='2';
                COMMIT;
            """)
        except BaseException:
            if self.db.in_transaction: self.db.rollback()
            self.db.close()
            raise
        if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise UserError("Database integrity check failed; restore a backup and reconcile.")

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    def close(self):
        self.db.close()

    def rows(self, sql: str, params=()) -> list[dict]:
        return [dict(row) for row in self.db.execute(sql, params).fetchall()]

    def one(self, sql: str, params=()) -> dict | None:
        value = self.db.execute(sql, params).fetchone()
        return dict(value) if value else None

    def event(self, kind: str, entity: str, data: dict):
        self.db.execute("INSERT INTO events(kind,entity,data,created) VALUES(?,?,?,?)",
                        (kind, entity, canonical(data), now()))

    def require_asset(self, asset_id):
        asset = self.one("SELECT * FROM assets WHERE id=?", (asset_id,))
        if not asset:
            raise UserError("Video not found in this workspace.")
        return asset

    def work_id(self, asset_id: str) -> str:
        asset = self.require_asset(asset_id)
        member = self.one("SELECT work_id FROM work_members WHERE asset_id=?", (asset_id,))
        if member: return member["work_id"]
        self.db.execute("INSERT OR IGNORE INTO works VALUES(?,?)", (asset_id, asset["created"]))
        self.db.execute("INSERT OR IGNORE INTO work_members VALUES(?,?)", (asset_id, asset_id))
        return self.one("SELECT work_id FROM work_members WHERE asset_id=?", (asset_id,))["work_id"]

    def work_job(self, asset_id: str, destination_id: str) -> dict | None:
        return self.one("SELECT j.* FROM work_destination_claims c JOIN jobs j ON j.id=c.job_id WHERE c.work_id=? AND c.destination_id=?",
                        (self.work_id(asset_id), destination_id))

    def reserve_work(self, asset_id: str, destination_id: str, job_id: str):
        work = self.work_id(asset_id)
        existing = self.work_job(asset_id, destination_id)
        if existing and existing["id"] != job_id:
            raise UserError("This work already has a publication cycle for that account; use its existing receipt/recovery.")
        self.db.execute("INSERT INTO work_destination_claims VALUES(?,?,?,?) ON CONFLICT(work_id,destination_id) DO UPDATE SET updated=excluded.updated",
                        (work, destination_id, job_id, now()))

    def release_work_claim(self, job_id: str):
        job = self.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not job: return
        safe = job["state"] == "cancelled" or (job["state"] == "failed" and self.meta("nonpublication:"+job_id)=="1") or (job["state"] == "failed" and not job["provider_id"] and
            (not job["submitted"] or bool(re.search(r"Zernio returned HTTP (400|401|403|422)\.",job["error"] or ""))))
        if safe: self.db.execute("DELETE FROM work_destination_claims WHERE job_id=?", (job_id,))

    def add_obligations(self, asset_id: str, destinations: list[str]):
        for destination in destinations:
            self.db.execute("INSERT OR IGNORE INTO obligations VALUES(?,?,?,?)",
                            (asset_id, destination, "pending", now()))

    def sync_accounts(self, accounts: list[dict], profiles: list[str], auto_enroll=False,
                      future_backfill=True, complete=True) -> dict:
        if not profiles:
            raise UserError("Select verified Zernio profiles before syncing accounts.")
        if not complete:
            raise UserError("Account listing is incomplete. Existing account state was preserved.")
        if not isinstance(accounts,list): raise UserError("Account inventory must be a complete list.")
        identifiers=[]
        for value in accounts:
            if not isinstance(value,dict): raise UserError("Malformed account inventory; state preserved.")
            profile=value.get("profileId")
            profile=profile.get("_id") if isinstance(profile,dict) else profile
            if profile not in profiles or not isinstance(value.get("_id"),str) or not isinstance(value.get("platform"),str):
                raise UserError("Account inventory has missing or out-of-scope identities; state preserved.")
            if not value["_id"].strip() or not re.fullmatch(r"[a-z][a-z0-9_]*",value["platform"]): raise UserError("Provider identity/platform is invalid; state preserved.")
            for field in ("platformUserId","displayName","username"):
                if value.get(field) is not None and not isinstance(value[field],str): raise UserError("Provider account identity fields must be text.")
            identifiers.append(value["_id"])
        if len(set(identifiers))!=len(identifiers): raise UserError("Duplicate account identities; state preserved.")
        seen, added = [], []
        with self.transaction():
            for value in accounts:
                profile = value.get("profileId")
                profile = profile.get("_id") if isinstance(profile, dict) else profile
                if profile not in profiles:
                    continue
                provider_id, platform = value.get("_id"), value.get("platform")
                if not provider_id or not platform:
                    raise UserError("Account response lacks identity; no changes committed.")
                native = value.get("platformUserId")
                native_key = f"{profile}:{platform}:{native}" if native else None
                found = self.one("SELECT * FROM destinations WHERE provider_id=?", (provider_id,))
                if not found and native_key:
                    found = self.one("SELECT * FROM destinations WHERE native_key=?", (native_key,))
                identity = found["id"] if found else str(uuid.uuid4())
                connected = value.get("isActive") is True and value.get("connectionStatus") != "disconnected"
                label = value.get("displayName") or value.get("username") or platform
                if found:
                    if found["platform"] != platform or found["profile_id"] != profile:
                        raise UserError("Account changed workspace/platform identity; review before relinking.")
                    if found["native_key"] and native_key and found["native_key"] != native_key:
                        raise UserError("Account native identity changed; existing history cannot be assigned to a different account.")
                    self.db.execute("UPDATE destinations SET provider_id=?,native_key=COALESCE(native_key,?),label=?,connected=?,updated=? WHERE id=?",
                                    (provider_id, native_key, label, int(connected), now(), identity))
                else:
                    # Capability is intentionally unknown until verified/configured.
                    self.db.execute("INSERT INTO destinations(id,provider_id,native_key,profile_id,platform,label,enabled,connected,updated) VALUES(?,?,?,?,?,?,?,?,?)",
                                    (identity, provider_id, native_key, profile, platform, label, int(auto_enroll), int(connected), now()))
                    added.append(identity)
                    if auto_enroll and future_backfill:
                        for asset in self.rows("SELECT id FROM assets WHERE library=1"):
                            self.add_obligations(asset["id"], [identity])
                    self.event("account-discovered", identity, {"platform": platform, "enabled": auto_enroll})
                seen.append(identity)
            for destination in self.rows("SELECT id,profile_id FROM destinations"):
                if destination["profile_id"] in profiles and destination["id"] not in seen:
                    self.db.execute("UPDATE destinations SET connected=0,updated=? WHERE id=?", (now(), destination["id"]))
        return {"added": added, "seen": seen}

    def set_destination(self, identity: str, *, enabled=None, capability=None, future_backfill=True):
        if not self.one("SELECT id FROM destinations WHERE id=?", (identity,)):
            raise UserError("Unknown account; sync accounts first.")
        with self.transaction():
            if enabled is not None:
                self.db.execute("UPDATE destinations SET enabled=?,updated=? WHERE id=?", (int(enabled), now(), identity))
                if enabled and future_backfill:
                    for asset in self.rows("SELECT id FROM assets WHERE library=1"):
                        self.add_obligations(asset["id"], [identity])
            if capability is not None:
                validate_capability(capability)
                self.db.execute("UPDATE destinations SET capability=?,updated=? WHERE id=?", (canonical(capability), now(), identity))
            self.event("account-policy", identity, {"enabled": enabled, "capability": capability})

    def coverage(self, asset_id: str) -> dict:
        asset = self.require_asset(asset_id)
        rows = self.rows("""SELECT o.destination_id,o.status,d.platform,d.label,d.enabled,d.connected
            FROM obligations o JOIN destinations d ON d.id=o.destination_id
            WHERE o.asset_id=? ORDER BY d.platform,d.label""", (asset_id,))
        # Show shared proof with its true originating job/asset; never copy a receipt
        # or falsely mark a second upload's obligations as directly published.
        for row in rows:
            proof=self.work_job(asset_id,row['destination_id'])
            if proof and proof['state']=='verified':
                row['status']='verified'
                row['proof_job_id']=proof['id'];row['proof_asset_id']=proof['asset_id']
        hold=self.one('SELECT status,data FROM repeat_holds WHERE asset_id=?',(asset_id,))
        active = [row for row in rows if row["enabled"]]
        return {"asset_id": asset_id, "work_id":self.work_id(asset_id), "repeat_hold":hold, "state": asset["state"], "library": bool(asset["library"]),
                "destinations": rows, "complete": bool(active) and all(row["status"] == "verified" for row in active)}

    def backup(self, target: Path):
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        output = sqlite3.connect(target)
        try:
            self.db.backup(output)
        finally:
            output.close()

    def meta(self, key: str, default=None):
        row=self.one("SELECT value FROM meta WHERE key=?",(key,))
        return row["value"] if row else default

    def set_meta(self, key: str, value: str | None):
        if value is None:
            self.db.execute("DELETE FROM meta WHERE key=?",(key,))
        else:
            self.db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,value))
