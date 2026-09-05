#!/usr/bin/python
"""
Bulk download script for ASF / NASA Earthdata data.
Author: Y-W. Chen
Original: Dec. 23, 2024
Updated:  Sep. 5, 2026 (Earthdata Cloud / URS OAuth compatibility, safe resume)

Usage
-----
    python ./bulk_downloader.py download-all-2026-09-04_16-01-37.py -o ./data
    python ./bulk_downloader.py URL [URL ...]
    python ./bulk_downloader.py downloads.metalink products.csv urls.txt
    python ./bulk_downloader.py -i urls.txt -o ./data

An ASF Vertex ``download-all-*.py`` script can be handed in directly; its
product list is extracted by static parsing and the file is never executed.

Run ``python ./bulk_downloader.py --help`` for all options.

Requirements: Python >= 3.10, ``requests``.

Licensed under the MIT License.  See LICENSE.
Provided "as is".  Users are responsible for adhering to the terms of service
of ASF DAAC and NASA Earthdata.
"""

from __future__ import annotations

import argparse
import base64
import csv
import getpass
import hashlib
import json
import os
import re
import stat
import sys
import time
import ast
import xml.etree.ElementTree as ET
from http.cookiejar import LoadError, MozillaCookieJar
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

try:
    import requests
except ImportError:  # pragma: no cover - environment problem, not logic
    sys.stderr.write("This script requires the 'requests' library: pip install requests\n")
    raise SystemExit(1)


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

USER_AGENT = "asf-bulk-downloader/2.0 (python-requests)"

# ASF's registered Earthdata Login (URS4) OAuth application.  These values are
# public; they are the same ones the official ASF bulk download script uses.
URS_AUTHORIZE_URL = "https://urs.earthdata.nasa.gov/oauth/authorize"
ASF_CLIENT_ID = "BO_n7nTIlMljdvU6kRRB3g"
ASF_REDIRECT_URI = "https://auth.asf.alaska.edu/login"
URS_PROFILE_URL = "https://urs.earthdata.nasa.gov/profile"

# Hosts that serve data out of Earthdata Cloud and expect an EDL bearer token
# rather than (only) a session cookie.
EDC_HOST_SUFFIXES = (".asf.earthdatacloud.nasa.gov",)

DEFAULT_COOKIE_JAR = os.path.join(os.path.expanduser("~"), ".bulk_download_cookiejar.txt")

CHUNK_SIZE = 1024 * 1024          # 1 MiB
CONNECT_TIMEOUT = 30              # seconds
READ_TIMEOUT = 120                # seconds; ASF/S3 can stall briefly
TIMEOUT = (CONNECT_TIMEOUT, READ_TIMEOUT)

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
MAX_RETRY_DELAY = 120            # seconds; ceiling for backoff and Retry-After
AUTH_STATUS = frozenset({401, 403})

EULA_HINT = (
    "\n  New/blocked users: log in to ASF Vertex (https://search.asf.alaska.edu) once,\n"
    "  accept the EULA for this dataset, and set your Study Area at\n"
    "  https://urs.earthdata.nasa.gov -- ASF will not serve data until you have."
)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def redact_url(url: str) -> str:
    """Strip query and fragment.

    Presigned S3 URLs carry credentials in the query string; printing them
    verbatim would leak a usable download token into terminal scrollback and
    log files.
    """
    try:
        p = urlparse(url)
    except ValueError:
        return "<unparsable url>"
    if p.query or p.fragment:
        return urlunparse((p.scheme, p.netloc, p.path, "", "", "")) + "?<redacted>"
    return url


# Query parameters that carry a usable credential.  These turn up inside
# requests' exception messages (which embed the URL that failed), so they must
# be scrubbed before anything is printed or written to a summary.
_SENSITIVE_PARAM_RE = re.compile(
    r"(?i)\b(X-Amz-Signature|X-Amz-Credential|X-Amz-Security-Token|X-Amz-Algorithm|"
    r"X-Amz-Date|X-Amz-Expires|X-Amz-SignedHeaders|X-Amz-Content-Sha256|Signature|"
    r"AWSAccessKeyId|token|access_token|id_token|refresh_token|code|"
    r"password|passwd|client_secret|api_key)=[^&\s'\"<>)]*")

_URL_IN_TEXT_RE = re.compile(r"https?://[^\s'\"<>]+")

_BEARER_RE = re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}")


def scrub(text) -> str:
    """Remove credentials from free-form text before printing or storing it.

    ``requests`` embeds the failing URL in its exception messages, so a dropped
    connection to a presigned S3 URL would otherwise print a working download
    signature straight into the terminal and the run summary.  The URL is often
    path-relative there ("Max retries exceeded with url: /f.h5?X-Amz-..."), so
    stripping whole URLs is not enough on its own.
    """
    text = str(text)
    text = _URL_IN_TEXT_RE.sub(lambda m: redact_url(m.group(0)), text)
    text = _SENSITIVE_PARAM_RE.sub(r"\1=<redacted>", text)
    text = _BEARER_RE.sub(r"\1<redacted>", text)
    return text


# Query parameters that are authentication material rather than object
# identity.  They are dropped before anything is written to disk.
_CREDENTIAL_QUERY_KEYS = frozenset({
    "x-amz-signature", "x-amz-credential", "x-amz-security-token", "x-amz-algorithm",
    "x-amz-date", "x-amz-expires", "x-amz-signedheaders", "x-amz-content-sha256",
    "signature", "awsaccesskeyid", "expires", "token", "access_token", "id_token",
    "refresh_token", "code", "state", "password", "passwd", "client_secret", "api_key",
})


def object_identity(url: str):
    """Split a URL into (durable identity, fingerprint of its remaining query).

    A presigned URL identifies an object by scheme/host/path; its query is
    short-lived credential material that changes on every request.  Storing the
    query verbatim would write an AWS signature and session token to disk, and
    would also break resume, because the next run's signature never matches.

    Any query parameter that is not a known credential is kept, but only as a
    SHA-256 fingerprint -- so a parameter that genuinely selects content (a
    burst subset, say) still distinguishes two objects, while a secret we did
    not recognise by name never lands on disk in readable form.
    """
    p = urlparse(url)
    base = urlunparse((p.scheme, p.netloc, p.path, "", "", ""))
    kept = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
            if k.lower() not in _CREDENTIAL_QUERY_KEYS]
    if not kept:
        return base, None
    canonical = urlencode(sorted(kept))
    return base, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def human_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024.0 or unit == "TiB":
            return f"{n:.2f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0
    return f"{n:.2f} TiB"


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._+@%,()\[\]{}=~-]")


def sanitize_filename(name: str) -> str | None:
    """Reduce a server- or URL-supplied name to a safe basename.

    Returns None when nothing usable is left.  Directory separators, ``..``
    and control characters can never survive this.
    """
    if not name:
        return None
    name = name.strip().strip('"').strip("'")
    name = name.replace("\\", "/").split("/")[-1]
    name = _SAFE_NAME_RE.sub("_", name)
    name = name.lstrip(".") if name.strip(".") == "" else name
    if name in ("", ".", ".."):
        return None
    return name[:255]


def fsync_dir(path: str) -> None:
    """Best-effort durability for a rename.  Not available on every platform."""
    try:
        fd = os.open(path, getattr(os, "O_DIRECTORY", os.O_RDONLY))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def filename_from_url(url: str) -> str | None:
    return sanitize_filename(os.path.basename(urlparse(url).path))


_CD_FILENAME_RE = re.compile(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', re.IGNORECASE)


def filename_from_headers(headers) -> str | None:
    cd = headers.get("Content-Disposition")
    if not cd:
        return None
    m = _CD_FILENAME_RE.search(cd)
    return sanitize_filename(m.group(1)) if m else None


def is_edc_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host.endswith(suffix) for suffix in EDC_HOST_SUFFIXES)


def decode_jwt_payload(token: str) -> dict:
    """Decode the (unverified) payload of a JWT-shaped token.

    We only read ``exp`` to avoid trying a download with an obviously stale
    token.  We never trust this for anything security-relevant -- the server
    is the authority.
    """
    parts = token.split(".")
    if len(parts) < 2:
        raise ValueError("not a JWT")
    payload = parts[1]
    payload += "=" * ((4 - len(payload) % 4) % 4)
    return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))


class DownloadError(Exception):
    """Non-retryable failure for a single file."""


class TransientError(Exception):
    """Retryable failure for a single file."""


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------

class EarthdataAuth:
    """Earthdata Login (URS4) session backed by a persistent cookie jar."""

    def __init__(self, cookie_jar_path: str = DEFAULT_COOKIE_JAR, verify_ssl: bool = True):
        self.cookie_jar_path = cookie_jar_path
        self.verify_ssl = verify_ssl

        self.jar = MozillaCookieJar(cookie_jar_path)
        if os.path.isfile(cookie_jar_path):
            try:
                self.jar.load(ignore_discard=True, ignore_expires=True)
            except (LoadError, OSError, ValueError) as exc:
                print(f" > Ignoring unreadable cookie jar {cookie_jar_path}: {exc}")
                self.jar = MozillaCookieJar(cookie_jar_path)

        self.session = requests.Session()
        self.session.cookies = self.jar          # persist straight into the jar
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.session.verify = verify_ssl
        # Content-Length must describe bytes on the wire for resume arithmetic
        # to be sound, so never let the server transfer-encode the payload.
        self.session.headers["Accept-Encoding"] = "identity"

    # -- cookie jar -------------------------------------------------------

    def _save_jar(self) -> None:
        """Persist the jar with owner-only permissions.

        The jar holds a live EDL bearer token, so it must not be readable by
        other users on the machine.
        """
        directory = os.path.dirname(self.cookie_jar_path) or "."
        os.makedirs(directory, exist_ok=True)
        # Create the file privately *before* anything is written to it.
        fd = os.open(self.cookie_jar_path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        try:
            os.chmod(self.cookie_jar_path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass  # e.g. some Windows/WSL mounts
        # Session cookies (asf-urs, urs-access-token) are discard=True; without
        # ignore_discard they would never be written and every run would need a
        # fresh login.  Validity is judged from the token's own 'exp' below,
        # not from the placeholder expiry MozillaCookieJar writes.
        self.jar.save(ignore_discard=True, ignore_expires=True)

    def _cookie(self, name: str):
        for cookie in self.jar:
            if cookie.name == name:
                return cookie
        return None

    def bearer_token(self) -> str | None:
        """The EDL access token used for Earthdata Cloud requests."""
        cookie = self._cookie("urs-access-token")
        if cookie and cookie.value:
            return cookie.value
        asf = self._cookie("asf-urs")
        if asf and asf.value:
            try:
                return decode_jwt_payload(asf.value).get("urs-access-token")
            except (ValueError, KeyError, json.JSONDecodeError):
                return None
        return None

    def token_expiry(self) -> float | None:
        token = self.bearer_token()
        if not token:
            return None
        try:
            exp = decode_jwt_payload(token).get("exp")
            return float(exp) if exp is not None else None
        except (ValueError, TypeError, json.JSONDecodeError):
            return None

    def jar_looks_logged_in(self) -> bool:
        """Cheap, offline check: do we hold an unexpired login cookie?"""
        exp = self.token_expiry()
        if exp is not None:
            # Avoid starting a request with an expired or nearly expired token.
            return exp > time.time() + 300
        # Older login flows only leave this marker behind.
        return self._cookie("urs_user_already_logged") is not None

    # -- login ------------------------------------------------------------

    def ensure_authenticated(self, max_attempts: int = 3) -> None:
        # Judged on the stored token's own expiry.  /profile is not consulted:
        # it requires a URS browser session that this OAuth flow never creates,
        # so using it here would force a password prompt on every single run.
        # A token that ASF later refuses still produces a clear 401/403.
        if self.jar_looks_logged_in():
            print(" > Reusing previous Earthdata cookie jar.")
            return

        if len(self.jar):
            print(" > Stored Earthdata cookie is missing or expired.")

        print("Earthdata Login required.")
        print("(Credentials are used only for this login and are never stored or logged.)")

        for attempt in range(1, max_attempts + 1):
            if self._login_once():
                print(" > Earthdata login successful.")
                return
            if attempt < max_attempts:
                print(f" > Login attempt {attempt}/{max_attempts} failed, try again.")
        raise SystemExit("Could not authenticate with NASA Earthdata Login. Aborting.")

    def _probe_profile(self) -> bool:
        """Confirm with URS that the cookie is actually still accepted.

        A network problem here is not treated as an authentication failure --
        we fall back to the offline cookie check so that a flaky link does not
        pointlessly prompt for a password.
        """
        try:
            resp = self.session.get(
                URS_PROFILE_URL, timeout=TIMEOUT, allow_redirects=False, stream=True
            )
            resp.close()
        except requests.RequestException as exc:
            print(f" > Could not reach URS to validate the cookie ({exc.__class__.__name__});"
                  " continuing with the stored one.")
            return True
        if resp.status_code in (200, 307):
            return True
        if resp.status_code in (301, 302, 303):
            # URS bounces anonymous users to the login form.
            return False
        if resp.status_code in AUTH_STATUS:
            return False
        print(f" > Unexpected status {resp.status_code} validating cookie; will re-authenticate.")
        return False

    def _login_once(self) -> bool:
        username = input("Earthdata username: ").strip()
        password = getpass.getpass("Earthdata password (not displayed): ")
        if not username or not password:
            print(" > Empty username or password.")
            return False

        # Start from an empty jar so a token left over from an earlier
        # (possibly anonymous) request cannot be mistaken for a successful
        # login below.
        self.jar.clear()

        basic = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        # Drop our reference to the password immediately.  Python strings are
        # immutable, so this cannot wipe it from memory -- it only shortens how
        # long it stays reachable.  It is never stored, echoed or logged.
        del password

        params = {
            "client_id": ASF_CLIENT_ID,
            "redirect_uri": ASF_REDIRECT_URI,
            "response_type": "code",
            "state": "",
        }
        try:
            # requests drops the Authorization header on cross-host redirects,
            # so the Basic credential never reaches auth.asf.alaska.edu or S3.
            resp = self.session.get(
                URS_AUTHORIZE_URL,
                params=params,
                headers={"Authorization": f"Basic {basic}"},
                timeout=TIMEOUT,
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            print(f" > Could not contact URS: {scrub(exc)}")
            return False
        finally:
            basic = None

        if resp.status_code in AUTH_STATUS:
            www_auth = resp.headers.get("WWW-Authenticate", "")
            if "Earthdata Login credentials" in www_auth or resp.status_code == 401:
                print(" > Username/password rejected by Earthdata Login.")
            else:
                print(" > Earthdata Login accepted the credentials but denied the ASF"
                      " application." + EULA_HINT)
            return False

        # A pre-existing token is not proof of login: an ordinary ANONYMOUS
        # request to ASF already yields an `asf-urs` cookie carrying an
        # unexpired urs-access-token.  The jar was therefore emptied before
        # this attempt, so any token present now was minted by this login.
        #
        # We deliberately do NOT gate on urs.earthdata.nasa.gov/profile here.
        # That page needs a URS *browser* session, which the OAuth Basic flow
        # does not create, so it can reject a session that is perfectly valid
        # for downloading from ASF.
        if not self.jar_looks_logged_in():
            print(f" > Login did not yield an Earthdata token "
                  f"(HTTP {resp.status_code})." + EULA_HINT)
            return False

        self._save_jar()
        return True

    def reauthenticate_for(self, auth_url: str) -> bool:
        """Follow a mid-download redirect back to URS to mint a fresh cookie.

        ASF sends this when an app-specific authorization is missing; the
        ``app_type=401`` hint makes URS answer with a status code instead of
        an HTML login page.
        """
        if "app_type" not in auth_url:
            auth_url += ("&" if "?" in auth_url else "?") + "app_type=401"
        print(" > Server redirected back to Earthdata Login; refreshing authorization.")
        token_before = self.bearer_token()
        try:
            resp = self.session.get(auth_url, timeout=TIMEOUT, allow_redirects=True)
        except requests.RequestException as exc:
            print(f" > Re-authorization request failed: {scrub(exc)}")
            return False
        # Re-authorization is only attempted because the token we hold was
        # refused, so success means a *different* token came back.  Getting the
        # same one (or none) means nothing was refreshed.  This avoids relying
        # on /profile, which the OAuth flow does not make accessible.
        if (resp.status_code >= 400 or not self.jar_looks_logged_in()
                or self.bearer_token() == token_before):
            print(f" > Re-authorization failed (HTTP {resp.status_code})." + EULA_HINT)
            return False
        self._save_jar()
        return True

    def headers_for(self, url: str) -> dict:
        """Per-request headers, adding the EDL bearer token only for EDC hosts."""
        headers = {}
        if is_edc_host(url):
            token = self.bearer_token()
            if token:
                headers["Authorization"] = f"Bearer {token}"
        return headers


# --------------------------------------------------------------------------
# Downloader
# --------------------------------------------------------------------------

class RemoteInfo:
    __slots__ = ("size", "etag", "last_modified", "filename", "accepts_ranges")

    def __init__(self, size=None, etag=None, last_modified=None, filename=None,
                 accepts_ranges=False):
        self.size = size
        self.etag = etag
        self.last_modified = last_modified
        self.filename = filename
        self.accepts_ranges = accepts_ranges


class BulkDownloader:
    def __init__(self, urls, auth: EarthdataAuth, output_dir: str = ".",
                 retries: int = 3, resume: bool = True, checksums: dict | None = None,
                 quiet: bool = False):
        self.urls = list(urls)
        self.auth = auth
        self.output_dir = output_dir
        self.retries = max(0, retries)
        self.resume = resume
        self.checksums = checksums or {}
        self.quiet = quiet

        self.total_bytes = 0
        self.total_time = 0.0
        self.success = []
        self.skipped = []
        self.failed = []

        os.makedirs(self.output_dir, exist_ok=True)
        if not os.access(self.output_dir, os.W_OK):
            raise SystemExit(f"Cannot write to output directory: {self.output_dir}")

    # -- HTTP -------------------------------------------------------------

    def _request(self, url, headers=None, stream=True):
        hdrs = self.auth.headers_for(url)
        if headers:
            hdrs.update(headers)
        return self.auth.session.get(
            url, headers=hdrs, stream=stream, timeout=TIMEOUT, allow_redirects=True
        )

    def _open_stream(self, url, extra_headers=None, allow_reauth=True):
        """GET *url*, transparently handling 202 waits and URS re-auth bounces."""
        for _ in range(12):  # bounded: at most ~1 minute of 202 polling
            try:
                resp = self._request(url, headers=extra_headers)
            except (requests.ConnectionError, requests.Timeout) as exc:
                raise TransientError(f"network error: {scrub(exc)}") from exc
            except requests.RequestException as exc:
                raise TransientError(f"request failed: {scrub(exc)}") from exc

            if resp.status_code == 202:
                resp.close()
                if not self.quiet:
                    print(" > Waiting for the burst extraction service...")
                time.sleep(5)
                continue

            # Earthdata Cloud bounces *every* request through the URS authorize
            # endpoint to mint a short-lived token, so simply passing through it
            # is normal and must not trigger a re-login.  It only signals a
            # problem when the request also ends badly.
            urs_url = None
            if URS_AUTHORIZE_URL in resp.url:
                urs_url = resp.url
            else:
                for hop in resp.history:
                    if URS_AUTHORIZE_URL in hop.url:
                        urs_url = hop.url
                        break

            # An HTML body where a product was expected means we were served a
            # login/error page.  This is the classic silent-corruption path.
            served_html = (resp.headers.get("Content-Type") or "").split(";")[0] \
                .strip().lower() in ("text/html", "application/xhtml+xml")

            needs_reauth = (
                resp.status_code == 401
                or (resp.status_code < 400 and served_html and urs_url is not None)
                or (resp.status_code < 400 and URS_AUTHORIZE_URL in resp.url)
            )

            if needs_reauth:
                resp.close()
                if not allow_reauth:
                    # We already refreshed the authorization once and are still
                    # being turned away, so this is a permission problem.
                    if served_html:
                        raise DownloadError(
                            "the server kept returning an Earthdata Login page instead of "
                            "data, even after refreshing authorization." + EULA_HINT)
                    raise DownloadError(
                        f"HTTP {resp.status_code}: not authorized for this product, even "
                        "after refreshing Earthdata authorization." + EULA_HINT)
                target = urs_url or (
                    f"{URS_AUTHORIZE_URL}?client_id={ASF_CLIENT_ID}"
                    f"&redirect_uri={ASF_REDIRECT_URI}&response_type=code&state=")
                if not self.auth.reauthenticate_for(target):
                    raise DownloadError("Earthdata re-authorization failed." + EULA_HINT)
                return self._open_stream(url, extra_headers, allow_reauth=False)

            if resp.status_code in AUTH_STATUS:
                resp.close()
                raise DownloadError(
                    f"HTTP {resp.status_code}: not authorized for this product." + EULA_HINT)

            if resp.status_code == 404:
                resp.close()
                raise DownloadError("HTTP 404: product not found at this URL.")

            if resp.status_code == 416:
                resp.close()
                raise DownloadError(
                    "HTTP 416: the server rejected our byte range. The remote object may "
                    "be empty, or a stale .part file may be larger than the product -- "
                    "delete any .part file for it and retry.")

            if resp.status_code in RETRYABLE_STATUS:
                retry_after = resp.headers.get("Retry-After")
                resp.close()
                err = TransientError(f"HTTP {resp.status_code} from server")
                # ASF rate-limits; respect an explicit Retry-After when sane.
                if retry_after and retry_after.strip().isdigit():
                    err.retry_after = min(int(retry_after.strip()), MAX_RETRY_DELAY)
                raise err

            if resp.status_code >= 400:
                resp.close()
                raise DownloadError(f"HTTP {resp.status_code} from server")

            return resp

        raise TransientError("server kept returning 202 (extraction not ready)")

    @staticmethod
    def _guard_content_type(resp, url):
        """Refuse to save an HTML login/error page as if it were a product.

        This is the classic silent-corruption path: an expired session yields a
        200 OK login page, which a naive downloader happily writes to
        ``S1A_...zip``.
        """
        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype in ("text/html", "application/xhtml+xml"):
            raise DownloadError(
                f"server returned an HTML page instead of data (Content-Type: {ctype}); "
                "this usually means the session expired or the EULA was not accepted."
                + EULA_HINT)

    def probe(self, url) -> RemoteInfo:
        """Cheaply learn size / validators / filename with a 1-byte ranged GET.

        HEAD is deliberately avoided: broken HEAD responses from the ASF
        datapool are the reason this project exists.
        """
        resp = self._open_stream(url, extra_headers={"Range": "bytes=0-0"})
        try:
            self._guard_content_type(resp, url)
            info = RemoteInfo(
                etag=resp.headers.get("ETag"),
                last_modified=resp.headers.get("Last-Modified"),
                # Prefer the name the server declares, then the URL the user
                # actually asked for; the post-redirect S3 key is the last
                # resort because it is the least predictable.
                filename=(filename_from_headers(resp.headers)
                          or filename_from_url(url)
                          or filename_from_url(resp.url)),
            )
            if resp.status_code == 206:
                info.accepts_ranges = True
                cr = resp.headers.get("Content-Range", "")
                m = re.match(r"bytes\s+\d+-\d+/(\d+)", cr)
                if m:
                    info.size = int(m.group(1))
            elif resp.status_code == 200:
                cl = resp.headers.get("Content-Length")
                if cl and cl.isdigit():
                    info.size = int(cl)
                info.accepts_ranges = "bytes" in (resp.headers.get("Accept-Ranges") or "")
            return info
        finally:
            resp.close()

    # -- part-file bookkeeping -------------------------------------------

    @staticmethod
    def _meta_path(part_path):
        return part_path + ".json"

    def _read_meta(self, part_path):
        try:
            with open(self._meta_path(part_path), "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    # Bumped to 3 when credential-bearing URLs stopped being stored.  A v2
    # sidecar is never trusted, so any old one (which may contain an AWS
    # signature) is discarded along with its .part rather than being reused.
    META_VERSION = 3

    def _write_meta(self, part_path, info: RemoteInfo, url):
        base, query_fp = object_identity(url)
        meta = {
            "url": base,                 # no query: never a signature or token
            "query_fingerprint": query_fp,
            "size": info.size,
            "etag": info.etag,
            "last_modified": info.last_modified,
            "version": self.META_VERSION,
        }
        tmp = self._meta_path(part_path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(meta, fh)
        os.replace(tmp, self._meta_path(part_path))

    def _discard_part(self, part_path, reason):
        if not self.quiet:
            print(f" > Discarding partial file ({reason}).")
        for path in (part_path, self._meta_path(part_path)):
            try:
                os.remove(path)
            except OSError:
                pass

    @classmethod
    def _meta_matches(cls, meta, info: RemoteInfo, url):
        """A partial file may only be resumed against the same bytes."""
        if not meta or meta.get("version") != cls.META_VERSION:
            return False
        base, query_fp = object_identity(url)
        if meta.get("url") != base or meta.get("query_fingerprint") != query_fp:
            return False
        if info.size is not None and meta.get("size") != info.size:
            return False
        # If either side offers a validator it must agree.
        for key, value in (("etag", info.etag), ("last_modified", info.last_modified)):
            if value is not None and meta.get(key) is not None and meta[key] != value:
                return False
        return True

    # -- integrity --------------------------------------------------------

    @staticmethod
    def md5sum(path):
        h = hashlib.md5()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(CHUNK_SIZE), b""):
                h.update(chunk)
        return h.hexdigest()

    def verify_checksum(self, path, filename):
        expected = self.checksums.get(filename)
        if not expected:
            return
        actual = self.md5sum(path)
        if actual.lower() != expected.lower():
            raise DownloadError(f"MD5 mismatch (expected {expected}, got {actual})")
        if not self.quiet:
            print(" > MD5 verified.")

    # -- progress ---------------------------------------------------------

    def _progress(self, done, total, force=False):
        if self.quiet:
            return
        now = time.time()
        if not force and now - getattr(self, "_last_progress", 0.0) < 0.5:
            return
        self._last_progress = now
        if total:
            pct = done / total * 100.0
            sys.stdout.write(f"\r   {human_bytes(done)} / {human_bytes(total)} ({pct:5.1f}%)   ")
        else:
            sys.stdout.write(f"\r   {human_bytes(done)} / unknown size   ")
        sys.stdout.flush()

    # -- the actual transfer ---------------------------------------------

    def _transfer(self, url, info: RemoteInfo, part_path, resume_from):
        """Stream to *part_path*.

        Returns ``(bytes_in_file, bytes_fetched_now)``.  The two differ when a
        download was resumed, and keeping them apart stops resumed bytes from
        being reported as freshly downloaded.

        Raises TransientError for anything worth retrying and DownloadError
        for anything that is not.
        """
        headers = {}
        mode = "wb"
        if resume_from > 0:
            headers["Range"] = f"bytes={resume_from}-"
            # If the object changed, the server must send the whole thing (200)
            # instead of a range we would have appended to the wrong bytes.
            validator = info.etag or info.last_modified
            if validator:
                headers["If-Range"] = validator

        resp = self._open_stream(url, extra_headers=headers)
        try:
            self._guard_content_type(resp, url)

            if resume_from > 0:
                if resp.status_code == 206:
                    cr = resp.headers.get("Content-Range", "")
                    m = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", cr)
                    if not m:
                        raise DownloadError(
                            f"server sent 206 without a usable Content-Range ({cr!r}); "
                            "refusing to append to avoid corrupting the file")
                    start = int(m.group(1))
                    total = int(m.group(3)) if m.group(3) != "*" else info.size
                    if start != resume_from:
                        raise DownloadError(
                            f"server resumed at byte {start}, we have {resume_from}; "
                            "refusing to append")
                    if info.size is not None and total is not None and total != info.size:
                        raise DownloadError(
                            "file changed on the server while resuming; "
                            "delete the .part file and start over")
                    mode = "ab"
                    expected_total = total if total is not None else info.size
                else:
                    # 200 here means the server ignored Range or If-Range failed.
                    # Appending would silently corrupt the file -- restart instead.
                    if not self.quiet:
                        print(f"\n > Server did not honour the resume request "
                              f"(HTTP {resp.status_code}); restarting this file from zero.")
                    resume_from = 0
                    mode = "wb"
                    cl = resp.headers.get("Content-Length")
                    expected_total = int(cl) if cl and cl.isdigit() else info.size
            else:
                if resp.status_code == 206:
                    raise DownloadError("unexpected 206 for a non-range request")
                cl = resp.headers.get("Content-Length")
                expected_total = int(cl) if cl and cl.isdigit() else info.size

            written = resume_from
            try:
                with open(part_path, mode) as fh:
                    if mode == "ab":
                        # Truncate any bytes beyond what we told the server we
                        # had (e.g. a crash between write and metadata update).
                        fh.seek(resume_from)
                        fh.truncate()
                    for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                        if not chunk:
                            continue
                        fh.write(chunk)
                        written += len(chunk)
                        if expected_total is not None and written > expected_total:
                            raise DownloadError(
                                f"server sent more data than announced "
                                f"({written} > {expected_total}); aborting")
                        self._progress(written, expected_total)
                    fh.flush()
                    os.fsync(fh.fileno())
            except requests.RequestException as exc:
                # Any mid-stream failure (dropped connection, read timeout,
                # truncated chunked encoding) is worth retrying; the bytes we
                # already have on disk stay valid.
                raise TransientError(f"transfer interrupted after "
                                     f"{human_bytes(written)}: {scrub(exc)}") from exc
            except OSError as exc:
                raise DownloadError(f"local file error: {scrub(exc)}") from exc

            self._progress(written, expected_total, force=True)
            if not self.quiet:
                sys.stdout.write("\n")

            if expected_total is None:
                raise DownloadError(
                    "server never announced a content length, so completeness cannot be "
                    "verified; refusing to accept this file")
            if written != expected_total:
                raise TransientError(
                    f"short transfer: got {written} of {expected_total} bytes")

            # Belt and braces: trust the filesystem, not our own counter.
            on_disk = os.path.getsize(part_path)
            if on_disk != expected_total:
                raise DownloadError(
                    f"wrote {written} bytes but the file holds {on_disk}; "
                    "refusing to treat this as complete")
            return written, written - resume_from
        finally:
            resp.close()

    # -- per-file orchestration ------------------------------------------

    def download_one(self, url, index, total_count):
        label = redact_url(url)
        print(f"\n({index}/{total_count}) {label}")

        try:
            info = self.probe(url)
        except (DownloadError, TransientError) as exc:
            # A probe failure that is transient is still worth one shot at the
            # real GET, but a hard failure is final.
            if isinstance(exc, DownloadError):
                print(f" ! {exc}")
                self.failed.append((label, str(exc)))
                return
            print(f" > Could not probe the file ({exc}); continuing without size info.")
            info = RemoteInfo(filename=filename_from_url(url))

        filename = info.filename or filename_from_url(url)
        if not filename:
            msg = "could not derive a safe local filename from this URL"
            print(f" ! {msg}")
            self.failed.append((label, msg))
            return

        final_path = os.path.join(self.output_dir, filename)
        part_path = final_path + ".part"

        # 1. Already have the finished product?
        if os.path.isfile(final_path):
            local_size = os.path.getsize(final_path)
            if info.size is None:
                print(f" > {filename} exists but the remote size is unknown; skipping. "
                      "Delete it to force a re-download.")
                self.skipped.append(filename)
                return
            if local_size == info.size:
                # Exact match only: a size tolerance would accept a truncated
                # product as complete.
                print(f" > {filename} already present and the size matches exactly; skipping.")
                try:
                    self.verify_checksum(final_path, filename)
                except DownloadError as exc:
                    print(f" ! {exc}")
                    self.failed.append((filename, str(exc)))
                    return
                self.skipped.append(filename)
                return
            print(f" > {filename} exists but is {local_size} bytes, expected {info.size}.")
            print("   Treating it as incomplete and re-downloading (the old file is kept "
                  f"as {filename}.incomplete until the new one succeeds).")
            try:
                os.replace(final_path, final_path + ".incomplete")
            except OSError as exc:
                msg = f"cannot move aside the existing file: {exc}"
                print(f" ! {msg}")
                self.failed.append((filename, msg))
                return

        # 2. Decide whether an existing .part can be resumed.
        resume_from = 0
        if os.path.isfile(part_path):
            if not self.resume:
                self._discard_part(part_path, "--no-resume")
            else:
                meta = self._read_meta(part_path)
                part_size = os.path.getsize(part_path)
                if not self._meta_matches(meta, info, url):
                    self._discard_part(part_path, "no matching resume metadata")
                elif info.size is not None and part_size >= info.size:
                    self._discard_part(part_path, "partial file is not smaller than the product")
                elif not info.accepts_ranges:
                    self._discard_part(part_path, "server does not advertise range support")
                else:
                    resume_from = part_size
                    print(f" > Resuming from {human_bytes(resume_from)}"
                          + (f" of {human_bytes(info.size)}." if info.size else "."))

        self._write_meta(part_path, info, url)

        # 3. Transfer, with a bounded retry budget.
        start = time.time()
        attempt = 0
        while True:
            attempt += 1
            try:
                written, fetched = self._transfer(url, info, part_path, resume_from)
                break
            except TransientError as exc:
                if attempt > self.retries:
                    msg = f"{exc} (gave up after {attempt} attempts)"
                    print(f" ! {msg}")
                    self.failed.append((filename, msg))
                    return
                delay = min(MAX_RETRY_DELAY,
                            getattr(exc, "retry_after", None) or 2 ** attempt)
                print(f" > {exc}; retry {attempt}/{self.retries} in {delay}s.")
                time.sleep(delay)
                if self.resume and os.path.isfile(part_path) and info.accepts_ranges:
                    resume_from = os.path.getsize(part_path)
                else:
                    resume_from = 0
            except DownloadError as exc:
                print(f" ! {exc}")
                print(f"   The partial file was kept at {os.path.basename(part_path)}; "
                      "it is NOT a usable product.")
                self.failed.append((filename, str(exc)))
                return
            except KeyboardInterrupt:
                print(f"\n > Interrupted. Partial data kept in "
                      f"{os.path.basename(part_path)}; rerun to resume.")
                raise

        # 4. Verify the .part while it is still clearly marked incomplete, and
        #    only then promote it.  Verifying after the rename would leave a
        #    known-bad file sitting under the real product name for a while --
        #    and permanently if the process were killed in between.
        try:
            self.verify_checksum(part_path, filename)
        except DownloadError as exc:
            bad = final_path + ".corrupt"
            try:
                os.replace(part_path, bad)
            except OSError:
                bad = part_path
            msg = f"{exc}; quarantined as {os.path.basename(bad)}"
            print(f" ! {msg}")
            self.failed.append((filename, msg))
            return

        try:
            os.replace(part_path, final_path)
        except OSError as exc:
            msg = f"could not move the completed file into place: {scrub(exc)}"
            print(f" ! {msg}")
            self.failed.append((filename, msg))
            return
        fsync_dir(self.output_dir)

        for leftover in (self._meta_path(part_path), final_path + ".incomplete"):
            try:
                os.remove(leftover)
            except OSError:
                pass

        elapsed = max(time.time() - start, 1e-6)
        rate = fetched / elapsed / 1024 ** 2
        resumed = written - fetched
        detail = f" ({human_bytes(resumed)} reused)" if resumed else ""
        print(f" > Done: {filename} ({human_bytes(written)}){detail} in {elapsed:.1f}s "
              f"({rate:.2f} MB/s)")
        self.total_bytes += fetched
        self.total_time += elapsed
        self.success.append((filename, written))

    def run(self):
        total = len(self.urls)
        for i, url in enumerate(self.urls, 1):
            try:
                self.download_one(url, i, total)
            except KeyboardInterrupt:
                print("\nAborted by user.")
                break
            except Exception as exc:  # never let one bad URL kill the batch
                msg = f"{exc.__class__.__name__}: {scrub(exc)}"
                print(f" ! Unexpected error: {msg}")
                self.failed.append((redact_url(url), msg))

    def print_summary(self):
        print("\n" + "-" * 78)
        print("Download summary")
        print("-" * 78)
        print(f"  Downloaded : {len(self.success)} file(s), {human_bytes(self.total_bytes)}")
        for name, size in self.success:
            print(f"      + {name}  ({human_bytes(size)})")
        if self.skipped:
            print(f"  Skipped    : {len(self.skipped)} file(s) already present")
            for name in self.skipped:
                print(f"      = {name}")
        if self.failed:
            print(f"  Failed     : {len(self.failed)} file(s)")
            for name, why in self.failed:
                print(f"      - {name}: {why}")
        if self.success and self.total_time > 0:
            rate = self.total_bytes / self.total_time / 1024 ** 2
            print(f"  Average rate: {rate:.2f} MB/s")
        print("-" * 78)
        return 1 if self.failed else 0


# --------------------------------------------------------------------------
# Input parsing
# --------------------------------------------------------------------------

ASF_SCRIPT_CLASS = "bulk_downloader"


def parse_asf_script(path):
    """Extract the product URLs from an ASF Vertex ``download-all-*.py`` script.

    The file is parsed as data with :mod:`ast` and is **never executed**: it is
    not imported, exec'd, eval'd or run as a subprocess.  Handing a downloader a
    Python file must not give that file the ability to run code.

    Only one structure is accepted -- the one ASF's generator actually emits::

        class bulk_downloader:
            def __init__(self):
                self.files = [ "https://.../PRODUCT.h5", ... ]

    Nothing else is inferred.  A ``self.files`` in an unrelated class, a
    module-level ``files`` list, a computed list, or two competing product
    lists are all rejected rather than guessed at, because queueing the wrong
    URLs is worse than reporting that the file was not understood.

    Only statements written directly in ``__init__`` are considered; a
    ``self.files`` inside a nested function or class is not ASF's structure and
    is ignored.  ASF's own script also assigns ``self.files`` a second time
    (``self.files = download_files``) inside a conditional when the user passes
    it a metalink or CSV; that is neither a direct statement nor a literal list,
    so it is not treated as a rival candidate.

    Returns the product URLs in their original order, de-duplicated, or an empty
    list with an explanation.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            source = fh.read()
    except OSError as exc:
        print(f"WARNING: could not read {path}: {exc}")
        return []

    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        print(f"WARNING: {path} is not valid Python (line {exc.lineno}); "
              "cannot extract product URLs.")
        return []
    except (RecursionError, MemoryError, ValueError) as exc:
        # A pathological file can exhaust the parser; refuse it rather than crash.
        print(f"WARNING: could not parse {path}: {exc.__class__.__name__}")
        return []

    def reject(reason):
        print(f"WARNING: {path} is not a recognised ASF download script "
              f"({reason}); no URLs extracted.")
        return []

    # -- locate class bulk_downloader (top level only) ---------------------
    classes = [n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == ASF_SCRIPT_CLASS]
    if not classes:
        return reject(f"no top-level `class {ASF_SCRIPT_CLASS}`")
    if len(classes) > 1:
        return reject(f"{len(classes)} classes named `{ASF_SCRIPT_CLASS}`")

    inits = [n for n in classes[0].body
             if isinstance(n, ast.FunctionDef) and n.name == "__init__"]
    if not inits:
        return reject(f"`{ASF_SCRIPT_CLASS}` has no `__init__`")
    if len(inits) > 1:
        return reject(f"`{ASF_SCRIPT_CLASS}.__init__` is defined {len(inits)} times")

    # -- find self.files = <literal list> inside that __init__ -------------
    def is_self_files(target):
        return (isinstance(target, ast.Attribute) and target.attr == "files"
                and isinstance(target.value, ast.Name) and target.value.id == "self")

    # Only statements written directly in __init__ count.  Walking the whole
    # subtree would also match a `self.files` inside a nested helper function
    # or a nested class, which is not ASF's structure.
    literal_lists, impure = [], []
    for node in inits[0].body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if not any(is_self_files(t) for t in targets):
            continue
        if not isinstance(value, (ast.List, ast.Tuple)):
            # e.g. `self.files = download_files` -- not a product list.
            continue
        items = [e.value for e in value.elts
                 if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        if len(items) != len(value.elts):
            impure.append(node.lineno)      # a list we cannot read in full
        else:
            literal_lists.append((node.lineno, items))

    if impure:
        return reject(f"the product list at line {impure[0]} is not a plain "
                      "list of string literals")
    if not literal_lists:
        return reject(f"`{ASF_SCRIPT_CLASS}.__init__` has no literal "
                      "`self.files = [...]` product list")
    if len(literal_lists) > 1:
        lines = ", ".join(str(ln) for ln, _ in literal_lists)
        return reject(f"several competing product lists (lines {lines})")

    urls, seen, rejected = [], set(), 0
    for item in literal_lists[0][1]:
        item = item.strip()
        if not item.startswith(("http://", "https://")):
            rejected += 1
            continue
        if item not in seen:
            seen.add(item)
            urls.append(item)

    if rejected:
        print(f" > Ignored {rejected} non-HTTP entr{'y' if rejected == 1 else 'ies'} "
              f"in {os.path.basename(path)}.")
    if not urls:
        print(f"WARNING: No ASF product URLs found in {path}.")
    return urls


def parse_metalink(path):
    urls = []
    try:
        tree = ET.parse(path)
    except (ET.ParseError, OSError) as exc:
        print(f"WARNING: could not parse metalink {path}: {exc}")
        return urls
    for el in tree.iter():
        if el.tag.split("}")[-1] == "url" and el.text:
            urls.append(el.text.strip())
    return urls


def parse_csv(path):
    urls = []
    try:
        with open(path, "r", newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            field = None
            for name in (reader.fieldnames or []):
                if name and name.strip().lower() == "url":
                    field = name
                    break
            if field is None:
                print(f"WARNING: no 'URL' column in {path}; skipping.")
                return urls
            for row in reader:
                value = (row.get(field) or "").strip()
                if value:
                    urls.append(value)
    except (csv.Error, OSError, UnicodeDecodeError) as exc:
        print(f"WARNING: could not read csv {path}: {exc}")
    return urls


def parse_url_list(path):
    urls = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    urls.append(line)
    except OSError as exc:
        print(f"WARNING: could not read {path}: {exc}")
    return urls


def load_checksums(path):
    """Read an ``md5sum``-style file: ``<md5><spaces><filename>`` per line."""
    table = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(None, 1)
                if len(parts) != 2:
                    continue
                digest, name = parts[0], parts[1].lstrip("*").strip()
                if re.fullmatch(r"[0-9a-fA-F]{32}", digest):
                    table[os.path.basename(name)] = digest.lower()
    except OSError as exc:
        raise SystemExit(f"Could not read checksum file {path}: {exc}")
    return table


def collect_urls(args):
    urls = list(URLS)
    for item in args.inputs + args.input_file:
        if item.startswith(("http://", "https://")):
            urls.append(item)
        elif item.endswith(".metalink") or item.endswith(".meta4"):
            urls.extend(parse_metalink(item))
        elif item.endswith(".csv"):
            urls.extend(parse_csv(item))
        elif item.endswith(".py"):
            urls.extend(parse_asf_script(item))
        elif os.path.isfile(item):
            urls.extend(parse_url_list(item))
        else:
            print(f"WARNING: '{item}' is neither a URL nor an existing file; ignoring.")

    seen, unique = set(), []
    for url in urls:
        if not url.startswith(("http://", "https://")):
            print(f"WARNING: ignoring non-HTTP entry: {url[:80]}")
            continue
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


# --------------------------------------------------------------------------
# Optional hardcoded list of URLs, used when no input is given on the
# command line.
# --------------------------------------------------------------------------

URLS = [
    # "https://datapool.asf.alaska.edu/SLC/SA/S1A_IW_SLC__xxxx1.zip",
]


def build_parser():
    p = argparse.ArgumentParser(
        prog="bulk_downloader.py",
        description="Lightweight bulk downloader for ASF / NASA Earthdata products.",
        epilog="Inputs may be an ASF Vertex download-all-*.py script (its product "
               "list is read without executing the file), a .metalink or .csv "
               "from Vertex, a plain text file with one URL per line, or URLs.",
    )
    p.add_argument("inputs", nargs="*", default=[],
                   help="URLs and/or ASF download-all-*.py / .metalink / .csv / .txt files")
    p.add_argument("-i", "--input-file", action="append", default=[], metavar="FILE",
                   help="additional input file (may be repeated)")
    p.add_argument("-o", "--output-dir", default=".", help="where to write products (default: .)")
    p.add_argument("--retries", type=int, default=3,
                   help="retry attempts per file for transient errors (default: 3)")
    p.add_argument("--no-resume", action="store_true",
                   help="always restart partial downloads from zero")
    p.add_argument("--checksums", metavar="FILE",
                   help="md5sum-style file used to verify downloaded products")
    p.add_argument("--cookie-jar", default=DEFAULT_COOKIE_JAR,
                   help=f"cookie jar path (default: {DEFAULT_COOKIE_JAR})")
    p.add_argument("--insecure", action="store_true",
                   help="do not verify TLS certificates (use only with a trusted source)")
    p.add_argument("--quiet", action="store_true", help="suppress the progress display")
    p.add_argument("--no-login", action="store_true",
                   help="skip Earthdata login; only works for openly accessible files "
                        "(some ASF metadata), and fails clearly on anything else")
    p.add_argument("--logout", action="store_true",
                   help="delete the stored cookie jar and exit")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.logout:
        try:
            os.remove(args.cookie_jar)
            print(f"Removed {args.cookie_jar}")
        except FileNotFoundError:
            print("No stored cookie jar to remove.")
        except OSError as exc:
            raise SystemExit(f"Could not remove cookie jar: {exc}")
        return 0

    urls = collect_urls(args)
    if not urls:
        print("No URLs to download.\n"
              "Pass an ASF download script, an input file, or URLs, e.g.:\n"
              "    python ./bulk_downloader.py download-all-2026-09-04_16-01-37.py -o ./data\n"
              "    python ./bulk_downloader.py downloads.metalink -o ./data\n"
              "    python ./bulk_downloader.py https://.../S1A_....zip")
        return 2

    if args.insecure:
        print("WARNING: TLS certificate verification is DISABLED.")
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass

    checksums = load_checksums(args.checksums) if args.checksums else {}

    auth = EarthdataAuth(cookie_jar_path=args.cookie_jar, verify_ssl=not args.insecure)
    if args.no_login:
        # A few ASF resources (notably .iso.xml metadata) are served without
        # Earthdata Login.  Anything else will fail with a clear 401/403 rather
        # than silently producing a login page, so this is safe to offer.
        print("Skipping Earthdata login (--no-login); "
              "products requiring authentication will fail.")
    else:
        auth.ensure_authenticated()

    downloader = BulkDownloader(
        urls, auth,
        output_dir=args.output_dir,
        retries=args.retries,
        resume=not args.no_resume,
        checksums=checksums,
        quiet=args.quiet,
    )
    print(f"\nQueued {len(urls)} file(s) -> {os.path.abspath(args.output_dir)}")
    downloader.run()
    return downloader.print_summary()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)

