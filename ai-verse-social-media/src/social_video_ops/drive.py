from __future__ import annotations

import os
import hashlib
import mimetypes
import socket
import ssl
import time
from pathlib import Path

from .util import UserError, atomic_write, file_hash

TRANSIENT_HTTP_STATUS = {408, 429, 500, 502, 503, 504}


def is_transient_google_error(exc: Exception) -> bool:
    # Adapted from captured social_drive_ops.py; only retry transient reads/updates.
    if isinstance(exc, (TimeoutError, socket.timeout, ConnectionError, ssl.SSLError)):
        return True
    return getattr(getattr(exc, "resp", None), "status", None) in TRANSIENT_HTTP_STATUS


def execute_with_retry(request, attempts=4, base_delay=1.0, sleep=time.sleep):
    for attempt in range(attempts):
        try:
            return request.execute()
        except Exception as exc:
            if not is_transient_google_error(exc) or attempt == attempts-1:
                raise
            sleep(base_delay * (2**attempt))


def connect(client_file: Path, token_file: Path):
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        raise UserError("Install the drive extra before connecting Google Drive.") from None
    flow = InstalledAppFlow.from_client_secrets_file(str(client_file), ["https://www.googleapis.com/auth/drive"])
    credentials = flow.run_local_server(port=0)
    atomic_write(token_file, credentials.to_json())
    return {"connected": True, "token_file": str(token_file)}


class Drive:
    def __init__(self, config: dict, timeout: float = 60, refresh_credentials: bool = True, retry_attempts: int = 4):
        self.config = config
        self.retry_attempts=retry_attempts
        try:
            import httplib2
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request
            from googleapiclient.discovery import build
            from google_auth_httplib2 import AuthorizedHttp
        except ImportError:
            raise UserError("Install the drive extra to enable Google Drive.") from None
        token_path = Path(os.environ.get("AI_VERSE_GOOGLE_TOKEN") or config.get("credentials_file") or "").expanduser()
        if not token_path.is_file():
            raise UserError("Google credentials not found. Use drive-connect or an existing authorized-user token.")
        self.token_path = token_path
        credentials = Credentials.from_authorized_user_file(str(token_path))
        if credentials.expired and not refresh_credentials:
            raise UserError("Google Drive authorization is expired; the read-only audit will not refresh credentials. Reconnect Drive.")
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            atomic_write(token_path, credentials.to_json())
        if not credentials.valid:
            raise UserError("Google Drive authorization expired; reconnect.")
        refresh_status_codes=(401,) if refresh_credentials else ()
        authorized_http=AuthorizedHttp(credentials,http=httplib2.Http(timeout=timeout),refresh_status_codes=refresh_status_codes)
        self.service = build("drive", "v3", http=authorized_http, cache_discovery=False)

    def identity(self):
        return execute_with_retry(self.service.about().get(fields="user,storageQuota"),attempts=self.retry_attempts)

    def metadata(self, identity):
        return execute_with_retry(self.service.files().get(fileId=identity, supportsAllDrives=True,
        fields="id,name,mimeType,size,md5Checksum,parents,trashed,modifiedTime,version,capabilities,webViewLink,appProperties"),attempts=self.retry_attempts)

    def generate_file_id(self):
        result = execute_with_retry(self.service.files().generateIds(count=1, space="drive"), attempts=self.retry_attempts)
        values = result.get("ids") if isinstance(result, dict) else None
        if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], str) or not values[0]:
            raise UserError("Google Drive did not provide a safe file identifier; no archive upload was started.")
        return values[0]

    @staticmethod
    def _md5(path: Path) -> str:
        value = hashlib.md5()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                value.update(block)
        return value.hexdigest()

    def _optional_metadata(self, file_id):
        try:
            return self.metadata(file_id)
        except Exception as exc:
            if getattr(getattr(exc, "resp", None), "status", None) == 404:
                return None
            raise UserError("Google Drive file state could not be checked; retry filing after reconnecting or checking Drive access.") from None

    def ensure_uploaded(self, path: Path, parent: str, file_id: str, asset_id: str, sha256: str):
        """Create or reconcile one archive copy under a locally persisted Drive file ID."""
        path = Path(path)
        if not path.is_file() or path.stat().st_size == 0:
            raise UserError("The local archive source is missing or empty; no Drive upload was attempted.")
        if file_hash(path) != sha256:
            raise UserError("The local archive source changed; review the asset before uploading it.")
        existing = self._optional_metadata(file_id)
        if existing is not None:
            props = existing.get("appProperties") or {}
            if (existing.get("id") != file_id or not existing.get("mimeType", "").startswith("video/")
                    or props.get("aiVerseAssetId") != asset_id or props.get("aiVerseSha256") != sha256
                    or int(existing.get("size", -1)) != path.stat().st_size
                    or (existing.get("md5Checksum") and existing["md5Checksum"] != self._md5(path))):
                raise UserError("The reserved Drive file ID contains different content; preserve it and review the archive record.")
            parents = existing.get("parents") or []
            if parent not in parents:
                if len(parents) != 1:
                    raise UserError("The Drive archive copy has an unexpected folder structure; review it before moving.")
                existing = self.move(file_id, parents[0], parent)
            if parent not in existing.get("parents", []):
                raise UserError("Drive did not confirm the archive folder; retry filing only.")
            return existing

        from googleapiclient.http import MediaFileUpload
        media = MediaFileUpload(str(path), resumable=True, chunksize=8 * 1024 * 1024,
                                mimetype=mimetypes.guess_type(path.name)[0] or "video/mp4")
        body = {"id": file_id, "name": path.name, "mimeType": mimetypes.guess_type(path.name)[0] or "video/mp4",
                "parents": [parent], "appProperties": {"aiVerseAssetId": asset_id, "aiVerseSha256": sha256}}
        try:
            request = self.service.files().create(body=body, media_body=media, supportsAllDrives=True,
                fields="id,name,mimeType,size,md5Checksum,parents,appProperties,webViewLink")
            result = None
            while result is None:
                _, result = request.next_chunk(num_retries=3)
        except Exception:
            # A lost response is reconciled by the pre-reserved ID on the next run.
            recovered = self._optional_metadata(file_id)
            if recovered is not None:
                return self.ensure_uploaded(path, parent, file_id, asset_id, sha256)
            raise UserError("Drive upload outcome is uncertain; retry filing with the same reserved file ID.") from None
        if (result.get("id") != file_id or parent not in result.get("parents", [])
                or int(result.get("size", -1)) != path.stat().st_size):
            raise UserError("Drive upload response did not prove the expected file, size, and archive folder.")
        if result.get("md5Checksum") and result["md5Checksum"] != self._md5(path):
            raise UserError("Drive upload checksum differs from the local archive source; review before retrying.")
        props = result.get("appProperties") or {}
        if props.get("aiVerseAssetId") != asset_id or props.get("aiVerseSha256") != sha256:
            raise UserError("Drive upload did not preserve its archive identity; review the remote file.")
        return result

    def inventory(self, folder_id, limit=100):
        values, token = [], None
        for _ in range(100):
            request = self.service.files().list(q=f"'{folder_id}' in parents and trashed=false", supportsAllDrives=True,
                       includeItemsFromAllDrives=True, pageSize=limit, pageToken=token,
                       fields="nextPageToken,files(id,name,mimeType,size,md5Checksum,parents,version,modifiedTime)")
            result = execute_with_retry(request)
            values.extend(result.get("files", []))
            token = result.get("nextPageToken")
            if not token:
                return values
        raise UserError("Folder inventory exceeded its page budget; narrow the source or resume in chunks.")

    def inventory_page(self, folder_id, page_token=None, limit=100, modified_after=None):
        escaped=str(folder_id).replace("\\","\\\\").replace("'","\\'")
        query=f"'{escaped}' in parents and trashed=false"
        if modified_after:
            query+=f" and modifiedTime > '{modified_after}'"
        request=self.service.files().list(q=query, supportsAllDrives=True,
            includeItemsFromAllDrives=True,pageSize=min(max(int(limit),1),100),pageToken=page_token,
            fields="nextPageToken,files(id,name,mimeType,size,md5Checksum,parents,version,modifiedTime)")
        result=execute_with_retry(request)
        return result.get("files",[]),result.get("nextPageToken")

    def download(self, identity: str, target: Path):
        import tempfile
        import hashlib
        from googleapiclient.http import MediaIoBaseDownload
        before = self.metadata(identity)
        if before.get("trashed") or not before.get("mimeType", "").startswith("video/"):
            raise UserError("Drive source is not an available video.")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(dir=target.parent, prefix=".download-")
        try:
            with os.fdopen(fd, "wb") as output:
                downloader = MediaIoBaseDownload(output, self.service.files().get_media(fileId=identity, supportsAllDrives=True), chunksize=8*1024*1024)
                complete = False
                while not complete:
                    _, complete = downloader.next_chunk(num_retries=3)
            after = self.metadata(identity)
            if after.get("trashed") or set(before.get("parents",[])) != set(after.get("parents",[])):
                raise UserError("Drive file left its approved folder during download; retry after review.")
            if before.get("version") != after.get("version"):
                raise UserError("Drive video changed while downloading; retry the final revision.")
            temporary = Path(name)
            if before.get("size") and temporary.stat().st_size != int(before["size"]):
                raise UserError("Downloaded byte count differs from Drive; source preserved.")
            if before.get("md5Checksum"):
                h = hashlib.md5()
                with temporary.open("rb") as file:
                    for block in iter(lambda: file.read(1024*1024), b""):
                        h.update(block)
                if h.hexdigest() != before["md5Checksum"]:
                    raise UserError("Downloaded checksum differs from Drive.")
            os.replace(temporary, target)
        finally:
            Path(name).unlink(missing_ok=True)
        return before

    def create_folder(self, name, parent):
        # Caller must inventory first; create is not automatically retried after a lost response.
        body = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent]}
        return self.service.files().create(body=body, fields="id,name,parents", supportsAllDrives=True).execute()

    def upload(self, path: Path, parent: str):
        from googleapiclient.http import MediaFileUpload
        import hashlib
        before = path.stat()
        media = MediaFileUpload(str(path), resumable=True, chunksize=8*1024*1024)
        request = self.service.files().create(body={"name": path.name, "parents": [parent]}, media_body=media,
                                               supportsAllDrives=True, fields="id,size,md5Checksum,parents")
        result = None
        # google client resumes chunks using the same session; never start a new create on timeout.
        while result is None:
            _, result = request.next_chunk(num_retries=3)
        if path.stat().st_mtime_ns != before.st_mtime_ns or int(result.get("size", -1)) != before.st_size:
            raise UserError("Upload needs reconciliation: local revision or remote size differs.")
        if parent not in result.get("parents", []):
            raise UserError("Uploaded video is not in the requested archive folder.")
        if result.get("md5Checksum"):
            h = file_hash(path,"md5")
            if h != result["md5Checksum"]:
                raise UserError("Uploaded checksum differs; reconcile before another upload.")
        return result

    def move(self, identity: str, expected_parent: str, target_parent: str):
        before = self.metadata(identity)
        if before.get("trashed"):
            raise UserError("Source was moved or removed externally; review before changing its parent.")
        if target_parent in before.get("parents", []):
            return before
        if expected_parent not in before.get("parents", []):
            raise UserError("Source was moved or removed externally; review before changing its parent.")
        # A timeout after this mutation is uncertain; re-read before considering another update.
        try:
            self.service.files().update(fileId=identity, addParents=target_parent,
                  removeParents=expected_parent, supportsAllDrives=True, fields="id,parents").execute()
        except Exception as exc:
            if not is_transient_google_error(exc):
                raise
        after = self.metadata(identity)
        if target_parent not in after.get("parents", []) or expected_parent in after.get("parents", []):
            raise UserError("Drive folder transition is not verified; retry sync, not publication.")
        return after
