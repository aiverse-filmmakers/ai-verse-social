from __future__ import annotations

import http.client
import hashlib
import ipaddress
import json
import mimetypes
import os
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .util import UserError, canonical, https_url, file_hash
from .caption_integrity import encode_payload


class ProviderError(UserError):
    def __init__(self, message, *, uncertain=False, status=None, code=None, retry_after=None, existing_id=None):
        super().__init__(message)
        self.uncertain, self.status, self.code = uncertain, status, code
        self.retry_after, self.existing_id = retry_after, existing_id


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward credentials or upload bytes to an unexpected redirect.
        return None


class Zernio:
    base = "https://zernio.com/api/v1"

    def __init__(self, timeout=60):
        self.timeout = timeout
        self.key = os.environ.get("ZERNIO_API_KEY")
        if not self.key:
            raise UserError("ZERNIO_API_KEY is missing. Connect Zernio using your host's secret storage.")
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method: str, path: str, data=None, key=None):
        headers = {"Authorization": f"Bearer {self.key}", "Accept": "application/json"}
        body = None
        if data is not None:
            body = encode_payload(data) if path == "/posts" and "content" in data else canonical(data).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        if key:
            headers["Idempotency-Key"] = key
        request = urllib.request.Request(self.base + path, body, headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                result=json.loads(response.read())
                if not isinstance(result,dict): raise ValueError("Expected an object")
                return result
        except urllib.error.HTTPError as exc:
            try:
                error = json.loads(exc.read())
            except ValueError:
                error = {}
            if not isinstance(error,dict): error={}
            # Keep safe classification; do not surface response bodies that could contain tokens/copy.
            raise ProviderError(f"Zernio returned HTTP {exc.code}. Check account access or provider diagnostics.",
                                uncertain=exc.code >= 500, status=exc.code, code=error.get("code"),
                                retry_after=exc.headers.get("Retry-After"), existing_id=error.get("existingPostId")) from None
        except (urllib.error.URLError, TimeoutError, socket.timeout, ValueError, http.client.HTTPException, OSError):
            raise ProviderError("Zernio response was unavailable or invalid; preserve the attempt and reconcile.", uncertain=True) from None

    def accounts(self, profiles: list[str]) -> list[dict]:
        if not profiles:
            raise UserError("Choose verified provider profiles first.")
        values = []
        # The documented unpaginated endpoint returns all accounts for the profile.
        for profile in profiles:
            response = self.request("GET", "/accounts?" + urllib.parse.urlencode({"profileId": profile, "includeOverLimit": "true"}))
            if not isinstance(response.get("accounts"), list) or response.get("hasMore"):
                raise UserError("Provider account listing is incomplete; no local account changes made.")
            if any(not isinstance(row, dict) or not row.get("_id") or not row.get("platform") for row in response["accounts"]):
                raise UserError("Provider account listing contains an invalid identity; no local account changes made.")
            for row in response["accounts"]:
                owner=row.get("profileId")
                owner=owner.get("_id") if isinstance(owner,dict) else owner
                if owner != profile: raise UserError("Provider inventory does not match the requested profile.")
            values.extend(response["accounts"])
        if len({row["_id"] for row in values}) != len(values):
            raise UserError("Provider inventory contains duplicate account identities.")
        return values

    def profiles(self):
        return self.request("GET", "/profiles")

    def health(self, account_id):
        return self.request("GET", f"/accounts/{urllib.parse.quote(account_id, safe='')}/health")

    def health_all(self, profiles: list[str]) -> list[dict]:
        if not profiles:
            raise UserError("Choose verified provider profiles first.")
        values = []
        for profile in profiles:
            response = self.request("GET", "/accounts/health?" + urllib.parse.urlencode({"profileId": profile}))
            rows = response.get("accounts")
            if not isinstance(rows, list) or any(not isinstance(row, dict) or not row.get("accountId") for row in rows):
                raise UserError("Provider account-health listing is incomplete; no account was marked ready.")
            values.extend(rows)
        if len({row["accountId"] for row in values}) != len(values):
            raise UserError("Provider account-health listing contains duplicate identities.")
        return values

    def upload(self, path: Path):
        original_hash=file_hash(path)
        size = path.stat().st_size
        if size > 5 * 1024**3:
            raise UserError("Video exceeds the provider's 5GB upload limit.")
        content_type = mimetypes.guess_type(path.name)[0] or "video/mp4"
        response = self.request("POST", "/media/presign", {"filename": path.name, "contentType": content_type, "size": size})
        upload_url = https_url(response.get("uploadUrl"))
        public_url = https_url(response.get("publicUrl"))
        parsed = urllib.parse.urlparse(upload_url)
        if parsed.port not in (None,443): raise UserError("Presigned upload must use the standard HTTPS port.")
        try:
            addresses=socket.getaddrinfo(parsed.hostname,443,type=socket.SOCK_STREAM)
        except OSError: raise ProviderError("Upload host could not be resolved.") from None
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise UserError("Presigned upload points to a nonpublic network address; upload refused.")
        address=addresses[0][4][0]
        class PinnedHTTPS(http.client.HTTPSConnection):
            def connect(self):
                sock=socket.create_connection((address,443),self.timeout)
                try: self.sock=self._context.wrap_socket(sock,server_hostname=self.host)
                except BaseException: sock.close(); raise
        connection = PinnedHTTPS(parsed.hostname,443,timeout=self.timeout,context=ssl.create_default_context())
        try:
            route = parsed.path + ("?" + parsed.query if parsed.query else "")
            connection.putrequest("PUT", route)
            connection.putheader("Content-Type", content_type)
            connection.putheader("Content-Length", str(size))
            connection.endheaders()
            sent_hash=hashlib.sha256()
            sent_bytes=0
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    sent_hash.update(block); sent_bytes+=len(block)
                    connection.send(block)
            if sent_bytes!=size or sent_hash.hexdigest()!=original_hash or file_hash(path)!=original_hash:
                raise UserError("Video changed during upload; no publication was submitted.")
            result = connection.getresponse()
            result.read()
            if not 200 <= result.status < 300:
                raise ProviderError(f"Media upload failed with HTTP {result.status}.")
        except (OSError,http.client.HTTPException):
            raise ProviderError("Media transfer failed; no publication was submitted.",uncertain=True) from None
        finally:
            connection.close()
        return public_url

    def validate(self, payload):
        response = self.request("POST", "/tools/validate/post", payload)
        if response.get("valid") is not True:
            raise UserError("Provider preflight failed; inspect the platform fields and caption limits.")
        return response

    def create(self, payload, key):
        return self.request("POST", "/posts", payload, key=key)

    def get(self, post_id):
        return self.request("GET", "/posts/" + urllib.parse.quote(post_id, safe=""))

    def cancel(self, post_id):
        return self.request("DELETE", "/posts/" + urllib.parse.quote(post_id, safe=""))

    def retry(self, post_id):
        return self.request("POST", "/posts/"+urllib.parse.quote(post_id,safe="")+"/retry")

    def update(self, post_id, payload):
        return self.request("PUT", "/posts/"+urllib.parse.quote(post_id,safe=""),payload)

    def best_times(self, account_id):
        return self.request("GET", "/analytics/best-time?" + urllib.parse.urlencode({"accountId": account_id}))


def extract_post(response: dict) -> dict:
    if not isinstance(response,dict): raise ProviderError("Provider returned malformed post data.",uncertain=True)
    if isinstance(response.get("post"), dict):
        return response["post"]
    if isinstance(response.get("data"), dict):
        return extract_post(response["data"])
    if response.get("_id"):
        return response
    if response.get("postId"):
        return {"_id": response["postId"], "status": "publishing", "platforms": []}
    raise ProviderError("Provider returned no usable post identifier; outcome is uncertain.", uncertain=True)
