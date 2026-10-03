from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from .config import validate
from .util import UserError, now, read_json, write_json, canonical, digest as payload_digest

FORMAT = "ai-verse-social-workspace"
VERSION = 1
MAX_RESTORE_BYTES = 20 * 1024**3
DATA_ROOTS = ("media", "logs", "captions", "requests", "transcripts", "branding")


def _hash(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def _workspace_files(root: Path) -> list[Path]:
    paths = [root / "settings.json", root / "onboarding-audit.json"]
    for name in DATA_ROOTS:
        directory = root / name
        if directory.exists():
            paths.extend(path for path in directory.rglob("*") if path.is_file())
            if any(path.is_symlink() for path in directory.rglob("*")):
                raise UserError("Workspace contains a linked file; move it inside the workspace before backing up.")
    for path in paths:
        if path.is_symlink():
            raise UserError("Workspace contains a linked file; move it inside the workspace before backing up.")
    files = sorted(path for path in paths if path.is_file())
    sensitive = ("secret", "credential", "token", "password", ".env")
    for path in files:
        name = path.name.lower()
        if any(marker in name for marker in sensitive) or path.suffix.lower() in {".pem", ".key"}:
            raise UserError("A secret-like file is inside the workspace data folders; move it outside before backing up.")
    return files


def create_backup(engine, target: Path) -> dict:
    root = engine.workspace.path.resolve()
    target = target.expanduser().resolve()
    if target == root or target.is_relative_to(root):
        raise UserError("Store workspace backups outside the live customer workspace.")
    if target.suffix.lower() != ".zip":
        raise UserError("Choose a .zip filename for the complete workspace backup.")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, db_name = tempfile.mkstemp(prefix=".social-backup-db-", dir=target.parent)
    os.close(fd)
    db_copy = Path(db_name)
    fd, archive_name = tempfile.mkstemp(prefix=".social-backup-", suffix=".zip", dir=target.parent)
    os.close(fd)
    partial = Path(archive_name)
    try:
        engine.store.backup(db_copy)
        files = {path.relative_to(root).as_posix(): path for path in _workspace_files(root)}
        files["state.sqlite3"] = db_copy
        manifest = {"format": FORMAT, "version": VERSION, "created_at": now(), "workspace_root": str(root),
                    "files": {name: _hash(path) for name, path in sorted(files.items())}}
        with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for name, path in sorted(files.items()):
                archive.write(path, name)
            archive.writestr("backup-manifest.json", json.dumps(manifest, indent=2) + "\n")
        os.chmod(partial, 0o600)
        os.replace(partial, target)
        return {"backup": str(target), "files": len(files), "sha256": _hash(target),
                "includes": ["settings", "database", *DATA_ROOTS],
                "credentials": "not bundled; reconnect host-managed secrets after restore"}
    except (OSError, sqlite3.Error, zipfile.BadZipFile) as exc:
        raise UserError(f"Workspace backup failed safely: {type(exc).__name__}.") from None
    finally:
        db_copy.unlink(missing_ok=True)
        partial.unlink(missing_ok=True)


def _safe_name(name: str) -> bool:
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        return False
    return path.parts[0] in {"settings.json", "onboarding-audit.json", "state.sqlite3", *DATA_ROOTS}


def restore_backup(source: Path, target: Path) -> dict:
    source = source.expanduser().resolve()
    target = target.expanduser().resolve()
    if not source.is_file() or source == target or source.is_relative_to(target):
        raise UserError("Choose a valid backup file and a separate, new workspace location.")
    if target.exists():
        raise UserError("Restore requires a new workspace path; existing files were left untouched.")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    stage = Path(tempfile.mkdtemp(prefix=".social-restore-", dir=target.parent))
    try:
        with zipfile.ZipFile(source, "r") as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise UserError("Backup contains duplicate paths; no files restored.")
            if sum(info.file_size for info in infos) > MAX_RESTORE_BYTES:
                raise UserError("Backup exceeds the safe restore size limit.")
            manifest_info = next((info for info in infos if info.filename == "backup-manifest.json"), None)
            if not manifest_info:
                raise UserError("Backup manifest is missing; no files restored.")
            manifest = json.loads(archive.read(manifest_info))
            if manifest.get("format") != FORMAT or manifest.get("version") != VERSION:
                raise UserError("Backup format/version is not supported; no files restored.")
            expected = manifest.get("files")
            if not isinstance(expected, dict) or "state.sqlite3" not in expected or "settings.json" not in expected:
                raise UserError("Backup is incomplete; no files restored.")
            if set(expected) != {info.filename for info in infos if info.filename != "backup-manifest.json"}:
                raise UserError("Backup contents do not match its manifest; no files restored.")
            for info in infos:
                if info.filename == "backup-manifest.json":
                    continue
                mode = info.external_attr >> 16
                if not _safe_name(info.filename) or (mode & 0o170000) == 0o120000:
                    raise UserError("Backup contains an unsafe path; no files restored.")
                destination = stage.joinpath(*PurePosixPath(info.filename).parts)
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                digest = hashlib.sha256()
                with archive.open(info) as incoming, destination.open("wb") as outgoing:
                    for block in iter(lambda: incoming.read(1024 * 1024), b""):
                        digest.update(block)
                        outgoing.write(block)
                if digest.hexdigest() != expected[info.filename]:
                    raise UserError("Backup checksum failed; no files restored.")
                os.chmod(destination, 0o600)
        settings = read_json(stage / "settings.json")
        original_root = manifest.get("workspace_root")
        def relocate(value):
            if isinstance(value, str) and original_root and Path(value).is_absolute():
                old = Path(original_root)
                path = Path(value)
                if path.is_relative_to(old):
                    relative = path.relative_to(old)
                    if relative.parts and relative.parts[0] in DATA_ROOTS:
                        if not (stage / relative).is_file():
                            raise UserError("Backup is missing a referenced customer file; restore refused.")
                        return str(target / relative)
            return value
        # Relocate only known filesystem fields, never arbitrary captions/provider bodies.
        options = settings.get("editing", {}).get("options", {})
        for key in ("watermark", "subtitles"):
            if key in options: options[key] = relocate(options[key])
        settings["paused"] = True
        settings = validate(settings)
        write_json(stage / "settings.json", settings)
        db_path = stage / "state.sqlite3"
        db = sqlite3.connect(db_path)
        try:
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise UserError("Backup database failed its integrity check; no files restored.")
            schema = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
            if not schema or schema[0] != "1":
                raise UserError("Backup database needs an unsupported migration; no files restored.")
            db.execute("PRAGMA journal_mode=DELETE")
            if not original_root:
                roots=set()
                for (path,) in db.execute("SELECT path FROM assets"):
                    value=Path(path)
                    if value.parent.name=="originals" and value.parent.parent.name=="media": roots.add(str(value.parent.parent.parent))
                    else: raise UserError("Legacy backup cannot safely relocate asset paths; upgrade and make a fresh backup.")
                if len(roots)>1: raise UserError("Legacy backup has multiple workspace roots; restore refused.")
                original_root=next(iter(roots),None)
            for identity, path, expected_hash in db.execute("SELECT id,path,hash FROM assets").fetchall():
                relocated=relocate(path)
                value=Path(relocated)
                if not value.is_relative_to(target) or _hash(stage/value.relative_to(target))!=expected_hash:
                    raise UserError("Backup media does not match its authoritative asset hash.")
                db.execute("UPDATE assets SET path=? WHERE id=?", (relocated,identity))
            for identity, raw, original_hash in db.execute("SELECT id,payload,payload_hash FROM requests").fetchall():
                payload=json.loads(raw)
                if payload_digest(payload)!=original_hash: raise UserError("Backup request authorization digest is invalid.")
                if "media" in payload: payload["media"]=relocate(payload["media"])
                for item in payload.get("targets",{}).values():
                    if "media" in item: item["media"]=relocate(item["media"])
                db.execute("UPDATE requests SET payload=?,payload_hash=? WHERE id=?",(canonical(payload),payload_digest(payload),identity))
            for identity, raw in db.execute("SELECT id,payload FROM jobs").fetchall():
                payload=json.loads(raw)
                if "media" in payload: payload["media"]=relocate(payload["media"])
                db.execute("UPDATE jobs SET payload=?,lease_owner=NULL,lease_until=NULL WHERE id=?",(canonical(payload),identity))
            for identity, raw in db.execute("SELECT id,data FROM actions").fetchall():
                data=json.loads(raw)
                # Pending archive paths need not already exist in the backup.
                path=data.get("path")
                if path and original_root and Path(path).is_relative_to(Path(original_root)):
                    data["path"]=str(target / Path(path).relative_to(Path(original_root)))
                db.execute("UPDATE actions SET data=? WHERE id=?",(canonical(data),identity))
            db.execute("INSERT INTO events(kind,entity,data,created) VALUES('restore-paths-relocated','workspace',?,?)",(canonical({"old_root":original_root,"new_root":str(target)}),now()))
            db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('restore_reconciliation_required','1')")
            db.commit()
        finally:
            db.close()
        (stage / "backups").mkdir(mode=0o700)
        for directory in sorted((path for path in stage.rglob("*") if path.is_dir()), key=lambda value: len(value.parts), reverse=True):
            os.chmod(directory, 0o700)
        os.chmod(stage, 0o700)
        os.replace(stage, target)
        return {"restored": str(target), "paused": True,
                "next": "reconnect host-managed secrets, audit, reconcile provider history, then explicitly resume"}
    except UserError:
        raise
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, sqlite3.Error) as exc:
        raise UserError(f"Restore failed safely: {type(exc).__name__}; existing workspace was not changed.") from None
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
