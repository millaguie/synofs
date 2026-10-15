#!/usr/bin/env python3
#
# synofs — Synology File Station CLI over the DSM Web API (QuickConnect-friendly)
# Copyright (C) 2026 millaguie
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# this program. If not, see <https://www.gnu.org/licenses/>.
"""
Synology File Station CLI over the DSM Web API (QuickConnect-friendly).

Designed for QuickConnect access (xxxxx.quickconnect.to), where SSH/rsync are
not available. Uses the DSM Web API (SYNO.API.Auth + SYNO.FileStation.*).

Credentials come from environment variables:
    SYNOLOGY_HOST       NAS URL, e.g. https://YOURID.quickconnect.to
    SYNOLOGY_USER       Synology username
    SYNOLOGY_PASSWORD   password (optional; prompted if unset)
(--host / --user override the env vars; the password is never taken from argv.)

Commands:
    shares                       list the shared folders you can access
    resolve                      show QuickConnect direct endpoints (diagnose --direct)
    qcinfo [--json]              dump what QuickConnect exposes (pre-login; privacy)
    ping                         check connectivity + login and show DSM identity
    info [--json]                dump everything the API exposes about the NAS
    stat REMOTE                  detailed metadata for a remote path
    md5  REMOTE                  NAS-computed MD5 checksum of a remote file
    search PATTERN [PATH]        recursively search the NAS by name pattern
    ls   [REMOTE_PATH]           list a remote directory (or the shares)
    du   REMOTE [-d N] [-a]      show disk usage of a file or directory
    mkdir REMOTE [-p]            create a folder on the NAS
    download REMOTE [DEST]       download a single file or a whole directory
    sync REMOTE_DIR [DEST]       recursively sync a remote directory into DEST
    upload LOCAL [REMOTE_DIR]    recursively upload a file or directory to the NAS

download/sync accept --verify to check each downloaded file against the
NAS-computed MD5 (marks a mismatch as 'corrupt').

download/sync auto-retry each file on a dropped connection, resuming from the
bytes already on disk (--retries, default 5). Pass --resume-only to finish just
the half-downloaded (*.part) files under DEST without fetching anything new.
Use -j/--jobs N to download N files in parallel (one live progress bar each).

Direction is explicit and never guessed. Every command except `upload`, `mkdir`
and `sync --delete` only ever READS from the NAS; `sync` writes locally, `upload`
writes remotely. Both transfer directions are additive by default (new/changed
files only, everything else untouched); pass --delete to turn either into a true
one-way mirror that also removes files on the destination. `upload --delete`
deletes files ON THE NAS, so it lists them and asks before doing it (--yes skips
the prompt for scripts). If the file-list walk was interrupted (Ctrl-C), the
mirror-delete pass is SKIPPED entirely: deleting against an incomplete list
could destroy good data.

sync mirrors the remote directory TREE, including empty directories; upload
skips symlinks (files and dirs), logging each skip.

Exit codes: 0 clean, 1 at least one item failed (error/corrupt/incomplete/
toolong) or a fatal error, 2 usage, 130 interrupted (Ctrl-C).

Uploads are NOT resumable: the File Station API has no chunked upload, so an
interrupted file restarts from zero on the next attempt (downloads do resume,
from their .part file).

Consistency / resume (applies to download and sync):
  - Local file exists with the SAME size and SAME date (mtime) -> skip.
  - A partial download exists and the remote matches the stored signature
    (size+date in the .part.json sidecar) -> RESUME with an HTTP Range request.
    If the server ignores Range (responds 200 instead of 206) -> re-download.
  - Remote changed (different size or date) -> download again.
  - Data goes to "<file>.part" and is only renamed (atomic os.replace) once
    complete: the final file is never left half-written and is not deleted
    until the new copy is ready.

sync path semantics (rsync-like trailing slash):
    sync /homes/user/dir1/dir2   .     ->  ./dir2/...   (creates the dir)
    sync /homes/user/dir1/dir2/  .     ->  ./...        (contents only)

Requirements:  pip install requests
Examples:
    export SYNOLOGY_HOST=https://YOURID.quickconnect.to
    export SYNOLOGY_USER=alice
    ./synofs.py ls /homes/alice
    ./synofs.py sync /homes/alice/dir1/dir2 .

References
----------
This tool wraps two bodies of knowledge that are NOT ours. Functions that rely
on either cite the tag below in a comment, so if the API/protocol changes you can
go straight back to the source.

  [FS-API]  Synology File Station Official API guide — the source for the
            SYNO.API.* / SYNO.FileStation.* endpoints, the /webapi/query.cgi +
            SYNO.API.Info discovery flow, and every error code below. Official
            (Synology developer docs):
    https://global.download.synology.com/download/Document/Software/DeveloperGuide/Package/FileStation/All/enu/Synology_File_Station_API_Guide.pdf

  [QC]      QuickConnect is NOT officially documented. The coordinator protocol
            (global.quickconnect.to/Serv.php + get_server_info, the server_info
            field layout, and the LAN -> WAN -> relay ordering) comes from these
            reverse-engineering write-ups / implementations:
    - nullvoidptr/qcon (Go lib) + protocol.md:
        https://github.com/nullvoidptr/qcon/blob/master/protocol.md
    - Thomas Theunen, "Diving into Synology QuickConnect" (2017):
        http://www.thomastheunen.eu/2017/07/diving-into-synology-quickconnect-and.html
    - mamascode, "Synology QuickConnect Protocol" (2015):
        https://mamascode.wordpress.com/2015/10/31/synology-quickconnect-protocol/
"""

import argparse
import concurrent.futures
import datetime
import getpass
import hashlib
import json
import logging
import os
import signal
import sys
import threading
import time

try:
    import requests
except ImportError:
    sys.exit("Missing 'requests'. Install with:  pip install requests")


CHUNK = 1 << 20          # 1 MiB per write block
READ_TIMEOUT = 60        # max seconds between received blocks
CONNECT_TIMEOUT = 30

log = logging.getLogger("synofs")


class _LevelFormatter(logging.Formatter):
    """Prefix each line by level so stderr reads like the old '-> ' diagnostics."""
    PREFIX = {
        logging.DEBUG: "·  ",
        logging.INFO: "-> ",
        logging.WARNING: "!  ",
        logging.ERROR: "error: ",
        logging.CRITICAL: "error: ",
    }

    def format(self, record):
        return self.PREFIX.get(record.levelno, "") + super().format(record)


def setup_logging(verbosity):
    """Wire the module logger to stderr. verbosity: <0 quiet, 0 info, >=1 debug."""
    level = logging.INFO
    if verbosity < 0:
        level = logging.WARNING
    elif verbosity >= 1:
        level = logging.DEBUG
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_LevelFormatter("%(message)s"))
    log.handlers[:] = [handler]
    log.setLevel(level)


# ---- graceful interruption ------------------------------------------------
# First Ctrl-C asks for a clean stop: in-flight transfers finish, nothing new
# starts, and every .part file stays resumable. A second Ctrl-C means the user is
# done being patient, so we stop being polite and exit immediately.

_interrupt = threading.Event()


def interrupted():
    """True once the user has asked for a clean stop."""
    return _interrupt.is_set()


def install_interrupt_handler():
    """Install the two-stage Ctrl-C handler (clean stop, then hard abort)."""
    def handler(_signum, _frame):
        if _interrupt.is_set():
            signal.signal(signal.SIGINT, signal.SIG_DFL)   # back to default
            sys.stderr.write(
                "\n!  Second Ctrl-C — aborting NOW. Partial files stay where "
                "they are and resume on the next run.\n")
            sys.stderr.flush()
            os._exit(130)
        _interrupt.set()
        sys.stderr.write(
            "\n!  Ctrl-C — dying cleanly: finishing the transfers already in "
            "flight and starting no new ones.\n"
            "!  Give me a moment, or press Ctrl-C again to die badly.\n")
        sys.stderr.flush()
    signal.signal(signal.SIGINT, handler)


class SynoError(Exception):
    pass


class IncompleteDownload(Exception):
    """Raised when a streaming pass ends before all bytes arrive (retryable)."""
    pass


# Common error messages from the DSM authentication API.
# Source: [FS-API] guide, SYNO.API.Auth error-code table (see module docstring).
AUTH_ERRORS = {
    400: "wrong username or password",
    401: "account disabled",
    402: "permission denied",
    403: "2FA (OTP) code required",
    404: "wrong 2FA (OTP) code",
    406: "password change / OTP enforcement required",
    407: "IP blocked",
}

# File Station (SYNO.FileStation.*) error codes.
# Source: [FS-API] guide, File Station API error-code table (see module docstring).
FS_ERRORS = {
    400: "invalid parameter",
    401: "unknown file-operation error",
    402: "system too busy",
    403: "user has no permission for this operation",
    406: "cannot get user/group info from the account server",
    407: "operation not permitted",
    408: "no such file or directory",
    409: "unsupported file system",
    410: "failed to connect to the internet-based file system",
    411: "read-only file system",
    414: "file already exists",
    415: "disk quota exceeded",
    416: "no space left on device",
    417: "input/output error",
    418: "illegal name or path (check the path exists and has no trailing slash; "
         "run 'shares' to see valid roots)",
    419: "illegal file name",
    421: "device or resource busy",
    599: "no such file-operation task",
}


def format_error(err):
    """Render an API error value (dict with 'code', or plain string)."""
    if isinstance(err, dict):
        code = err.get("code")
        hint = ""
        if isinstance(code, int):
            hint = FS_ERRORS.get(code) or AUTH_ERRORS.get(code) or ""
        return f"code {code} — {hint}" if hint else f"code {code}"
    return str(err)


def err_text(exc):
    """Human message for a SynoError/exception: render its payload if it has one."""
    return format_error(exc.args[0] if getattr(exc, "args", None) else exc)


def decode_json(resp, what):
    """Parse a response body as JSON, or raise SynoError with diagnostics.

    The DSM Web API (and the QuickConnect coordinator) always answer with JSON;
    anything else means we are not actually talking to the API. Instead of a bare
    JSONDecodeError, surface *what* came back: status, content-type, URL and a
    snippet of the body, plus a hint at the usual causes.
    """
    try:
        return resp.json()
    except ValueError:
        pass

    ctype = resp.headers.get("Content-Type", "?")
    body = (resp.text or "").strip()

    # QuickConnect bounced us to its "Unable to connect" error page: the relay
    # itself could not reach the NAS. This is a connectivity/config problem on
    # the NAS side, not a wrong URL — say so specifically.
    if "/portal/error.html" in resp.url or "Unable to connect QuickConnect" in body:
        code = resp.url.split("error=", 1)[1].split("&", 1)[0] if "error=" in resp.url else ""
        raise SynoError(
            f"{what}: QuickConnect could not connect to the NAS"
            f"{f' (relay error {code})' if code else ''}.\n"
            f"    The relay bounced the request to {resp.url}\n"
            f"    Likely causes: the NAS is powered off or offline; the "
            f"QuickConnect ID is wrong; the QuickConnect relay service is "
            f"disabled; or service permissions are not granted (Control Panel > "
            f"External Access > QuickConnect > Advanced).\n"
            f"    Try: confirm the NAS is on and reachable and the ID is correct, "
            f"then 'resolve' / '--direct' to try a direct (non-relay) path."
        )

    snippet = body[:200].replace("\n", " ") if body else "<empty body>"
    raise SynoError(
        f"{what}: expected JSON but got HTTP {resp.status_code} ({ctype}) "
        f"from {resp.url}\n"
        f"    body: {snippet}\n"
        f"    hint: the URL is probably not the DSM Web API — a QuickConnect "
        f"relay/landing page, a wrong --host, or a captive proxy. "
        f"Try the 'resolve' command to find a direct endpoint."
    )


def as_list(v):
    """Coerce a value to a list: None/'' -> [], a scalar -> [scalar], list -> list.

    QuickConnect's server_info mixes shapes (smartdns.lan is a list, .external a
    plain string), so callers normalize before iterating.
    """
    return [] if not v else (v if isinstance(v, list) else [v])


def clean_remote(path):
    """Normalize a remote path to '/a/b': leading slash, no trailing, no '//'.

    DSM rejects trailing slashes on most File Station calls, and repeated
    separators confuse path arithmetic, so normalize once here.
    """
    return "/" + "/".join(p for p in (path or "").split("/") if p)


# Filenames are limited by BYTES, not characters — the NAS filesystem (ext4/btrfs)
# caps a path segment at 255 bytes, so a Chinese name (3 bytes/char in UTF-8) hits
# the wall well before it looks long. 240 leaves headroom for temp-file overhead.
DEFAULT_MAX_NAME_BYTES = 240


def name_bytes(name):
    """UTF-8 byte length of a name (what the filename limit is measured in)."""
    return len(name.encode("utf-8"))


def fit_name(name, max_bytes):
    """Shorten `name` to fit `max_bytes` UTF-8 bytes, or return it unchanged.

    Preserves the extension, cuts the stem on a CHARACTER boundary (never mid
    UTF-8 sequence, so no mojibake), and appends a short deterministic hash of the
    original name — so two different over-long names don't collapse into one, and
    re-runs produce the SAME shortened name (which keeps skip/mirror stable).
    """
    if name_bytes(name) <= max_bytes:
        return name
    root, ext = os.path.splitext(name)
    if name_bytes(ext) > 40:                  # not a real extension
        root, ext = name, ""
    tag = "~" + hashlib.md5(name.encode("utf-8")).hexdigest()[:6]
    if name_bytes(tag) + name_bytes(ext) >= max_bytes:
        ext = ""                              # budget too small to keep the extension
    budget = max(1, max_bytes - name_bytes(tag) - name_bytes(ext))
    while name_bytes(root) > budget:          # trim one character at a time
        root = root[:-1]
    out = root + tag + ext
    while name_bytes(out) > max_bytes:        # pathological tiny budgets: the tag
        out = out[:-1]                        # itself may not fit — hard-trim
    return out


def fit_rel_path(rel, max_bytes):
    """Apply fit_name() to every segment of a relative path."""
    return "/".join(fit_name(seg, max_bytes) for seg in rel.split("/") if seg)


def rel_has_long_segment(rel, max_bytes):
    """True if any segment of a relative path exceeds the byte limit."""
    return any(name_bytes(seg) > max_bytes for seg in rel.split("/") if seg)


def human(n):
    """Human-readable byte size."""
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}T"


# ---- QuickConnect direct-path resolution ---------------------------------
# QuickConnect connects LAN -> port-forwarded WAN -> relay, in that order; the
# relay is only the slow fallback. Using the "xxx.quickconnect.to" URL forces
# the relay. Here we ask Synology's coordinator for the NAS's own addresses and
# probe them, so we can talk to the NAS directly and skip the relay entirely.
#
# EVERYTHING in this section (the coordinator URL, the Serv.php get_server_info
# request, the shape of the response, and the connection ordering) is based on
# the reverse-engineered [QC] references in the module docstring — QuickConnect
# has no official docs. If Synology changes the protocol, start there.

QC_COORDINATOR = "https://global.quickconnect.to"


def qc_extract_id(host):
    """Pull the QuickConnect ID out of a URL/hostname (or return it as-is).

    Everything left of '.quickconnect.to' is the ID — do NOT split on dots:
    subdomain-style IDs ('my.nas.quickconnect.to') are a single ID, 'my.nas'.
    """
    h = host.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    if h.endswith(".quickconnect.to"):
        return h[: -len(".quickconnect.to")]
    return h


def _qc_query(server_id, control=QC_COORDINATOR):
    # Request body / field names per [QC] (nullvoidptr/qcon protocol.md et al.).
    body = {
        "version": 1, "command": "get_server_info",
        "stop_when_error": False, "stop_when_success": False,
        "id": "dsm_portal_https", "serverID": server_id,
    }
    r = requests.post(f"{control}/Serv.php", json=body, timeout=10)
    r.raise_for_status()
    return decode_json(r, "QuickConnect coordinator")


def qc_resolve(server_id):
    """Return the coordinator's server_info dict for a QuickConnect ID.

    QuickConnect is regionally sharded: the global coordinator often does NOT
    hold the record and instead answers errno 4 ("Ds info not found") together
    with a `sites` list naming the regional coordinator(s) that do. We follow
    those, then follow env.control_host for the region-local (most accurate)
    answer. (Behaviour per the [QC] references.)
    """
    info = _qc_query(server_id)
    # Global has no record but points us at regional coordinator(s): follow them.
    if qc_error(info) and info.get("sites"):
        for site in info["sites"]:
            try:
                regional = _qc_query(server_id, f"https://{site}")
            except requests.RequestException:
                continue
            if not qc_error(regional):
                info = regional
                break
    control = (info.get("env") or {}).get("control_host")
    if control:
        try:
            local = _qc_query(server_id, f"https://{control}")
            if local.get("server"):
                info = local
        except requests.RequestException:
            pass
    return info


# get_server_info error codes (errno != 0). Names per the [QC] references; the
# raw 'errinfo' string is surfaced for anything not mapped here.
QC_ERRNO = {
    4: "no NAS is registered with this QuickConnect ID — wrong/mistyped ID, or "
       "the NAS was never set up or has been deregistered",
}


def qc_error(info):
    """Return a human message if the coordinator returned an error, else None."""
    errno = info.get("errno")
    if not errno:
        return None
    parts = [f"coordinator errno {errno}"]
    hint = QC_ERRNO.get(errno) or info.get("errinfo")
    if hint:
        parts.append(str(hint))
    return " — ".join(parts)


def qc_candidates(info):
    """Build ordered (label, base_url, verify_tls) direct-connection candidates.

    Ordered fastest/best first: SmartDNS (valid cert) -> LAN -> DDNS -> WAN IP.
    Raw-IP candidates carry verify=False (their cert won't match the IP).

    The server_info field layout read below (top-level `smartdns`, `service`;
    server.interface / server.ddns / server.external; service.port / ext_port) is
    documented in the [QC] references — this is the part most likely to drift if
    Synology changes the coordinator response, so go back there first if
    candidates come up empty. NOTE: `smartdns` is a TOP-LEVEL key (sibling of
    `server`), and its `lan`/`lanv6` are lists while `external`/`host` are plain
    strings — hence the as_list() coercion.
    """
    server = info.get("server") or {}
    service = info.get("service") or {}
    port = service.get("port") or 5001
    ext_port = service.get("ext_port") or port
    cands = []

    # SmartDNS FQDNs carry a valid *.direct.quickconnect.to cert, so verify=True.
    smart = info.get("smartdns") or server.get("smartdns") or {}
    for fqdn in as_list(smart.get("lan")):
        cands.append(("smartdns/lan", f"https://{fqdn}:{port}", True))
    for fqdn in as_list(smart.get("external")):
        cands.append(("smartdns/external", f"https://{fqdn}:{ext_port}", True))
    if smart.get("host"):
        cands.append(("smartdns", f"https://{smart['host']}:{port}", True))

    for itf in (server.get("interface") or []):
        ip = itf.get("ip")
        if ip and not ip.startswith("169.254"):
            cands.append((f"lan/{itf.get('name', 'if')}", f"https://{ip}:{port}", False))

    ddns = server.get("ddns")
    if ddns and ddns not in ("NULL", "", None):
        cands.append(("ddns", f"https://{ddns}:{ext_port}", True))

    ext_ip = (server.get("external") or {}).get("ip")
    if ext_ip:
        cands.append(("wan", f"https://{ext_ip}:{ext_port}", False))

    # de-dupe preserving order
    seen, out = set(), []
    for label, url, verify in cands:
        if url not in seen:
            seen.add(url)
            out.append((label, url, verify))
    return out


def qc_probe(url, verify, timeout=6):
    """True if the DSM Web API answers directly at `url`."""
    try:
        r = requests.get(
            f"{url}/webapi/query.cgi",
            params={"api": "SYNO.API.Info", "version": 1,
                    "method": "query", "query": "SYNO.API.Auth"},
            verify=verify, timeout=timeout,
        )
        return bool(r.ok and r.json().get("success"))
    except (requests.RequestException, ValueError):
        return False


def qc_find_direct(host):
    """Resolve a QuickConnect host to the first reachable direct endpoint.

    Returns (base_url, verify_tls, label) or None if no direct path works.
    """
    server_id = qc_extract_id(host)
    info = qc_resolve(server_id)
    err = qc_error(info)
    if err:
        log.warning("QuickConnect lookup rejected: %s", err)
        return None
    for label, url, verify in qc_candidates(info):
        if qc_probe(url, verify):
            return url, verify, label
    return None


class Synology:
    def __init__(self, base_url, verify=True):
        # Normalize: if no scheme is given, assume https.
        if not base_url.startswith(("http://", "https://")):
            base_url = "https://" + base_url
        self.base = base_url.rstrip("/")
        self.verify = verify
        self.session = requests.Session()
        self.apis = {}
        self.account = None

    # ---- API plumbing ----------------------------------------------------

    # APIs we cannot work without vs. ones that only power optional commands
    # (a DSM that hides them just disables the matching command, cleanly).
    REQUIRED_APIS = ("SYNO.API.Auth", "SYNO.FileStation.List",
                     "SYNO.FileStation.Download")
    OPTIONAL_APIS = ("SYNO.FileStation.Info", "SYNO.FileStation.MD5",
                     "SYNO.FileStation.Search", "SYNO.FileStation.Upload",
                     "SYNO.FileStation.CreateFolder", "SYNO.FileStation.Delete")

    def _get_json(self, url, params, what, conn_error=None):
        """GET `url` (standard verify/timeout), decode JSON and check success.

        One place for the request boilerplate shared by discover/call/all_apis:
        a dropped/refused connection becomes a SynoError, a non-JSON body is
        diagnosed by decode_json(), and an API-level {success:false} raises with
        the error payload (a dict, so callers like login() can read its 'code').
        """
        try:
            r = self.session.get(url, params=params, verify=self.verify,
                                  timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            r.raise_for_status()
        except requests.RequestException as e:
            raise SynoError(f"{conn_error or what + ' request failed'}: {e}")
        j = decode_json(r, what)
        if not j.get("success"):
            raise SynoError(j.get("error"))
        return j

    def _query_url(self):
        return f"{self.base}/webapi/query.cgi"

    _clean = staticmethod(clean_remote)          # see clean_remote() above

    def discover(self):
        """Look up the path and max version of every API we need.

        The /webapi/query.cgi + SYNO.API.Info handshake (and using the returned
        path/maxVersion for every later call) is the mandatory bootstrap
        described in [FS-API] ("Get Started"); see the module docstring.
        """
        wanted = ",".join(self.REQUIRED_APIS + self.OPTIONAL_APIS)
        log.debug("Discovering APIs (%s) at %s", wanted, self.base)
        j = self._get_json(
            self._query_url(),
            {"api": "SYNO.API.Info", "version": 1, "method": "query",
             "query": wanted},
            "SYNO.API.Info",
            conn_error=f"could not reach the DSM Web API at {self.base}")
        self.apis = j.get("data") or {}
        missing = [name for name in self.REQUIRED_APIS if name not in self.apis]
        if missing:
            raise SynoError("these required APIs are not available on this DSM: "
                            + ", ".join(missing))
        absent_opt = [n for n in self.OPTIONAL_APIS if n not in self.apis]
        if absent_opt:
            log.debug("Optional APIs not offered by this DSM: %s",
                      ", ".join(absent_opt))
        log.debug("Discovered %d APIs", len(self.apis))

    def require_api(self, api_name, command):
        """Raise a friendly error if an optional API is missing for a command."""
        if api_name not in self.apis:
            raise SynoError(f"the '{command}' command needs {api_name}, which this "
                            f"DSM does not expose")

    def _url(self, api_name):
        return f"{self.base}/webapi/{self.apis[api_name]['path']}"

    def _query(self, api_name, method, params=None, version=None):
        info = self.apis[api_name]
        ver = version or info["maxVersion"]
        q = {"api": api_name, "version": ver, "method": method}
        if params:
            q.update(params)
        return q

    # Params that must NEVER reach the logs: login sends the password (and the
    # OTP) as call params, and -v prints every call to stderr.
    SECRET_PARAMS = frozenset(("passwd", "password", "otp_code"))

    @classmethod
    def _redacted(cls, params):
        """A copy of `params` with secret values masked, for debug logging."""
        if not params or not cls.SECRET_PARAMS.intersection(params):
            return params
        return {k: ("***" if k in cls.SECRET_PARAMS else v)
                for k, v in params.items()}

    def call(self, api_name, method, params=None, version=None):
        """Normal call: returns the 'data' object of the JSON response."""
        log.debug("%s.%s %s", api_name, method, self._redacted(params))
        j = self._get_json(self._url(api_name),
                           self._query(api_name, method, params, version),
                           f"{api_name}.{method}")
        return j.get("data") or {}

    def call_stream(self, api_name, method, params=None, version=None,
                    headers=None):
        """Streaming call: returns the Response object as-is."""
        return self.session.get(
            self._url(api_name),
            params=self._query(api_name, method, params, version),
            headers=headers,
            verify=self.verify,
            stream=True,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        )

    # ---- auth ------------------------------------------------------------

    def login(self, account, passwd, otp=None):
        # Params (account/passwd/session/format/otp_code) and the login/logout
        # methods on SYNO.API.Auth v6 are per [FS-API] (see module docstring).
        params = {
            "account": account,
            "passwd": passwd,
            "session": "FileStation",
            "format": "cookie",
        }
        if otp:
            params["otp_code"] = otp
        log.debug("Logging in as %s%s", account, " (with OTP)" if otp else "")
        try:
            self.call("SYNO.API.Auth", "login", params, version=6)
        except SynoError as e:
            code = e.args[0].get("code") if isinstance(e.args[0], dict) else None
            if code in (403, 404) and not otp:
                try:
                    otp = input("2FA (OTP) code: ").strip()
                except EOFError:
                    raise SynoError(
                        "this account requires a 2FA (OTP) code but stdin is "
                        "not interactive — run synofs from a terminal") from None
                if not otp:
                    raise SynoError("empty 2FA (OTP) code")
                return self.login(account, passwd, otp)
            hint = AUTH_ERRORS.get(code, "") if isinstance(code, int) else ""
            raise SynoError(f"login failed (code {code}{': ' + hint if hint else ''})")
        self.account = account
        log.debug("Logged in as %s", account)

    def logout(self):
        try:
            self.call("SYNO.API.Auth", "logout",
                      {"session": "FileStation"}, version=6)
        except Exception as e:
            log.debug("logout failed (ignored): %s", e)

    # ---- listing / stat --------------------------------------------------

    def list_shares(self):
        data = self.call("SYNO.FileStation.List", "list_share",
                         {"additional": '["real_path"]'})
        return [s["path"] for s in data.get("shares", [])]

    @staticmethod
    def _entry(f):
        """Normalize a File Station file entry into a small dict."""
        add = f.get("additional", {})
        return {
            "path": f["path"],
            "name": f.get("name", os.path.basename(f["path"])),
            "isdir": bool(f.get("isdir")),
            "size": add.get("size", 0) or 0,
            "mtime": int(add.get("time", {}).get("mtime", 0) or 0),
        }

    def stat(self, path):
        """Return a small entry dict for a single path, or None if absent.

        Thin wrapper over getinfo(): swallows API errors (returns None) so callers
        like du/download/sync can treat "missing" and "unreadable" uniformly.
        """
        try:
            f = self.getinfo(path)
        except SynoError:
            return None
        return self._entry(f) if f else None

    def _paginate(self, fetch):
        """Collect normalized File Station 'files' across pages.

        `fetch(offset)` returns one page's response 'data'; we keep going in
        blocks of 1000 until a short page signals the end.
        """
        entries, offset = [], 0
        while True:
            files = fetch(offset).get("files", [])
            entries.extend(self._entry(f) for f in files)
            if len(files) < 1000:
                return entries
            offset += len(files)

    def listdir(self, folder_path):
        """Return the immediate entries of a remote directory."""
        folder_path = self._clean(folder_path)
        return self._paginate(lambda off: self.call(
            "SYNO.FileStation.List", "list", {
                "folder_path": folder_path, "offset": off, "limit": 1000,
                "additional": '["size","time","type"]'}))

    def walk(self, folder_path, dirs=None):
        """Yield file entry dicts for every file under folder_path (recursive).

        If `dirs` (a list) is given, every subdirectory entry encountered is
        also appended to it — callers that need the directory tree itself (e.g.
        sync, to recreate EMPTY remote dirs locally) collect it there.

        Stops early on Ctrl-C so a long remote walk does not have to finish
        before the clean shutdown can take effect.
        """
        for e in self.listdir(folder_path):
            if interrupted():
                return
            if e["isdir"]:
                if dirs is not None:
                    dirs.append(e)
                yield from self.walk(e["path"], dirs)
            else:
                yield e

    def getinfo(self, path):
        """Return the raw File Station file object for a path (with rich fields).

        The `additional` field names (real_path/size/owner/time/perm/type/…) are
        the ones enumerated for SYNO.FileStation.List in [FS-API].
        """
        path = self._clean(path)
        data = self.call("SYNO.FileStation.List", "getinfo", {
            "path": json.dumps([path]),
            "additional": '["real_path","size","owner","time","perm","type",'
                          '"mount_point_type"]',
        })
        files = data.get("files", [])
        if not files or files[0].get("code"):
            return None
        return files[0]

    # ---- server info / checksum / search --------------------------------

    def server_info(self):
        """Return SYNO.FileStation.Info 'get' data (hostname, capabilities, …).

        SYNO.FileStation.Info / method=get is per [FS-API] (module docstring).
        """
        self.require_api("SYNO.FileStation.Info", "ping")
        return self.call("SYNO.FileStation.Info", "get")

    def all_apis(self):
        """Query the FULL API catalog the DSM exposes (SYNO.API.Info query=all).

        The query="all" form of SYNO.API.Info is documented in [FS-API].
        """
        j = self._get_json(
            self._query_url(),
            {"api": "SYNO.API.Info", "version": 1, "method": "query",
             "query": "all"},
            "SYNO.API.Info(all)")
        return j.get("data") or {}

    def gather_info(self):
        """Collect everything we can read about the NAS into one dict.

        Non-admin sessions can't touch most SYNO.Core.* endpoints, so we stick to
        what a File Station user is allowed to see: the connection/session state,
        SYNO.FileStation.Info capabilities, and the full advertised API catalog.
        Sections that error out (e.g. permission denied) are recorded, not fatal.
        """
        out = {
            "connection": {
                "endpoint": self.base,
                "tls_verified": self.verify,
                "account": self.account,
            }
        }
        try:
            out["filestation_info"] = self.server_info()
        except SynoError as e:
            out["filestation_info"] = {"error": err_text(e)}
        try:
            out["apis"] = self.all_apis()
        except SynoError as e:
            out["apis"] = {"error": err_text(e)}
        return out

    def md5(self, remote_path, poll=0.5):
        """Ask the NAS to compute a file's MD5 (async task); return the digest.

        The start/status task flow and the `file_path`/`taskid`/`md5` fields are
        per [FS-API], SYNO.FileStation.MD5 (see module docstring). If this errors
        with 'invalid parameter', the parameter name is the thing to re-check.
        """
        self.require_api("SYNO.FileStation.MD5", "md5")
        remote_path = self._clean(remote_path)
        data = self.call("SYNO.FileStation.MD5", "start",
                         {"file_path": remote_path})
        taskid = data.get("taskid")
        if not taskid:
            raise SynoError("MD5: the NAS did not return a task id")
        while True:
            st = self.call("SYNO.FileStation.MD5", "status", {"taskid": taskid})
            if st.get("finished"):
                return st.get("md5")
            if interrupted():
                raise SynoError("MD5 task cancelled (Ctrl-C)")
            time.sleep(poll)

    def search(self, folder_path, pattern, recursive=True, poll=0.3):
        """Recursively search for `pattern` (name/glob) under folder_path.

        The async start -> poll list(finished) -> page results -> stop/clean flow
        and its parameters are per [FS-API], SYNO.FileStation.Search (see the
        module docstring).
        """
        self.require_api("SYNO.FileStation.Search", "search")
        data = self.call("SYNO.FileStation.Search", "start", {
            "folder_path": self._clean(folder_path),
            "pattern": pattern,
            "recursive": "true" if recursive else "false",
        })
        taskid = data.get("taskid")
        if not taskid:
            raise SynoError("search: the NAS did not return a task id")
        try:
            while True:                                # wait for the async scan
                st = self.call("SYNO.FileStation.Search", "list",
                               {"taskid": taskid, "offset": 0, "limit": 1})
                if st.get("finished"):
                    break
                if interrupted():
                    raise SynoError("search cancelled (Ctrl-C)")
                time.sleep(poll)
            return self._paginate(lambda off: self.call(    # then page the result
                "SYNO.FileStation.Search", "list", {
                    "taskid": taskid, "offset": off, "limit": 1000,
                    "additional": '["size","time","type"]'}))
        finally:
            for method in ("stop", "clean"):
                try:
                    self.call("SYNO.FileStation.Search", method, {"taskid": taskid})
                except SynoError:
                    pass

    # ---- writing to the NAS ---------------------------------------------
    # Everything below WRITES. The rest of this tool is read-only by design; keep
    # these confined to the 'upload' / 'mkdir' commands so that stays true for
    # every other command.

    def mkdir(self, path, force_parent=True):
        """Create a remote folder. Returns True if created, False if it existed.

        SYNO.FileStation.CreateFolder takes the PARENT folder plus the new name
        (both as JSON lists), and force_parent creates missing ancestors. [FS-API]
        """
        self.require_api("SYNO.FileStation.CreateFolder", "mkdir")
        path = self._clean(path)
        parent, name = os.path.dirname(path), os.path.basename(path)
        if not name:
            raise SynoError("refusing to create the root folder '/'")
        try:
            self.call("SYNO.FileStation.CreateFolder", "create", {
                "folder_path": json.dumps([parent or "/"]),
                "name": json.dumps([name]),
                "force_parent": "true" if force_parent else "false",
            })
            return True
        except SynoError as e:
            code = e.args[0].get("code") if isinstance(e.args[0], dict) else None
            if code == 414:                      # "file already exists"
                return False
            raise

    def delete(self, path):
        """Delete a remote file or folder (blocking form). [FS-API]"""
        self.require_api("SYNO.FileStation.Delete", "delete")
        self.call("SYNO.FileStation.Delete", "delete", {
            "path": json.dumps([self._clean(path)]),
            "recursive": "true",
        })

    def same_by_checksum(self, remote_path, local_path, size):
        """True if local already matches the remote by MD5, for --checksum mode.

        Unlike Nextcloud (checksum in the listing), the File Station API only
        computes MD5 on demand, so this costs one async md5 task on the NAS PER
        file — the price of --checksum on Synology. Falls back to a size check if
        the NAS can't produce a checksum.
        """
        if not os.path.exists(local_path):
            return False
        try:
            remote_md5 = self.md5(remote_path)
        except SynoError as e:
            log.warning("--checksum: the NAS produced no MD5 for %s (%s); "
                        "falling back to a SIZE-ONLY comparison for this file",
                        remote_path, err_text(e))
            return os.path.getsize(local_path) == size
        if not remote_md5:
            log.warning("--checksum: the NAS returned an empty MD5 for %s; "
                        "falling back to a SIZE-ONLY comparison for this file",
                        remote_path)
            return os.path.getsize(local_path) == size
        return _file_md5(local_path).lower() == remote_md5.lower()

    def upload_file(self, local_path, remote_dir, size, mtime, name=None,
                    retries=5, on_progress=None, log=print, remote_entry=None,
                    by_checksum=False):
        """Upload one local file into a remote folder. Returns a status string.

        NOTE: the File Station API has no chunked/resumable upload, so each file
        goes up in a single multipart POST — an interrupted transfer restarts that
        file from zero (unlike downloads, which resume from the .part file).

        The skip decision is size+mtime by default, or MD5 when by_checksum is set.

        Field ORDER matters: Synology's uploader expects the metadata fields
        first and the file content LAST, and `mtime` in MILLISECONDS. [FS-API]
        """
        self.require_api("SYNO.FileStation.Upload", "upload")
        name = name or os.path.basename(local_path)
        if remote_entry and not remote_entry["isdir"]:
            if by_checksum:
                if self.same_by_checksum(self._clean(f"{remote_dir}/{name}"),
                                         local_path, size):
                    return "skip"
            elif remote_entry["size"] == size and remote_entry["mtime"] == mtime:
                return "skip"

        attempt = 0
        while True:
            try:
                with open(local_path, "rb") as fh:
                    body = _ProgressReader(fh, size, on_progress)
                    fields = [
                        ("api", (None, "SYNO.FileStation.Upload")),
                        ("version", (None, "2")),
                        ("method", (None, "upload")),
                        ("path", (None, self._clean(remote_dir))),
                        ("create_parents", (None, "true")),
                        ("overwrite", (None, "true")),
                        ("mtime", (None, str(int(mtime) * 1000))),
                        ("file", (name, body, "application/octet-stream")),
                    ]
                    r = self.session.post(
                        self._url("SYNO.FileStation.Upload"), files=fields,
                        verify=self.verify,
                        timeout=(CONNECT_TIMEOUT, max(READ_TIMEOUT, 300)))
                r.raise_for_status()
                j = decode_json(r, "SYNO.FileStation.Upload")
                if not j.get("success"):
                    raise SynoError(j.get("error"))
                return "uploaded"
            except (requests.RequestException, IncompleteDownload) as ex:
                attempt += 1
                if attempt > retries:
                    return "incomplete"
                log(f"  ~ {name}: {type(ex).__name__}, retry {attempt}/{retries} "
                    f"(no resume for uploads: restarting the file)")
                time.sleep(min(2 ** attempt, 30))

    # ---- download --------------------------------------------------------

    def download_file(self, remote_path, size, mtime, local_path, retries=5,
                      on_progress=None, log=print, by_checksum=False):
        """Download one file with consistency checks and resume. Returns status.

        Resumes automatically WITHIN this call: if the connection drops or the
        stream ends short, it retries (up to `retries` times) picking up from
        wherever the .part file left off, with exponential backoff.

        The skip decision is size+mtime by default, or MD5 when by_checksum is set
        (one md5 task on the NAS per already-present file).

        `on_progress(done, total)` is called as bytes arrive (may be throttled by
        the caller); `log(msg)` emits one-off lines (retries). Both default so the
        method works standalone, but in parallel mode the caller wires them to a
        shared multi-line progress renderer.
        """
        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)

        # Already present and unchanged?
        if os.path.exists(local_path):
            if by_checksum:
                if self.same_by_checksum(remote_path, local_path, size):
                    return "skip"
            elif os.path.getsize(local_path) == size and \
                    int(os.path.getmtime(local_path)) == mtime:
                return "skip"
            # differs: continue, but do NOT delete the old file; replace at the end.

        part = local_path + ".part"
        meta = part + ".json"

        # Decide whether an existing .part can be safely resumed. We ONLY trust a
        # .part when it is paired with a sidecar whose recorded signature matches
        # the current remote file. Anything else (orphan .part with no sidecar,
        # orphan sidecar with no .part, mismatched signature, oversized/corrupt
        # partial) is wiped so we always start from a clean, verifiable state.
        pre_existing = False
        m = {}
        if os.path.exists(meta):
            try:
                with open(meta) as fh:
                    m = json.load(fh)
            except Exception:
                m = {}
        matches = m.get("size") == size and m.get("mtime") == mtime
        if os.path.exists(part) and matches:
            ps = os.path.getsize(part)
            if ps == size:
                self._finalize(part, meta, local_path, mtime)
                return "resumed"              # already whole, only the rename was missing
            if 0 < ps < size:
                pre_existing = True           # verified partial -> clean resume
            else:                              # empty or oversized -> not usable
                self._rm(part)
        else:
            # No usable pair: drop any orphan .part / stale sidecar.
            self._rm(part, meta)

        # (Re)write the sidecar with the current remote signature + path
        # (the remote path lets `--resume-only` finish it without a full walk).
        with open(meta, "w") as fh:
            json.dump({"remote": remote_path, "size": size, "mtime": mtime}, fh)

        attempt = 0
        while True:
            # Recompute the resume offset from the actual .part each attempt, so
            # a mid-file drop continues from the bytes already on disk.
            resume_from = os.path.getsize(part) if os.path.exists(part) else 0
            try:
                self._stream_once(remote_path, part, meta, size, resume_from,
                                  on_progress)
            except (requests.RequestException, IncompleteDownload) as ex:
                attempt += 1
                if attempt > retries:
                    return "incomplete"       # keep .part + sidecar for a later run
                have = os.path.getsize(part) if os.path.exists(part) else 0
                log(f"  ~ {os.path.basename(local_path)}: {type(ex).__name__}, "
                    f"retry {attempt}/{retries} resuming at {human(have)}")
                time.sleep(min(2 ** attempt, 30))
                continue
            self._finalize(part, meta, local_path, mtime)
            return "resumed" if pre_existing else "downloaded"

    def _stream_once(self, remote_path, part, meta, size, resume_from, on_progress):
        """One streaming pass into `part`. Raises IncompleteDownload if short."""
        headers = {"Range": f"bytes={resume_from}-"} if resume_from else None
        # path as a JSON list: the format the download endpoint expects.
        resp = self.call_stream("SYNO.FileStation.Download", "download", {
            "path": json.dumps([remote_path]),
            "mode": "download",
        }, headers=headers)

        ctype = resp.headers.get("Content-Type", "")
        if "application/json" in ctype:        # the API returned an error, not bytes
            resp_json = decode_json(resp, "download")
            self._rm(part, meta)
            raise SynoError(f"download rejected: {format_error(resp_json.get('error'))}")

        if resp.status_code not in (200, 206) or "text/html" in ctype.lower():
            # Not bytes: an HTTP error, OR an HTML login/landing page served with
            # status 200 (expired session, captive portal, relay error page).
            # NEVER write that into the .part — surface a real error instead.
            # The .part is untouched and stays resumable for a later run.
            try:
                snippet = next(resp.iter_content(256), b"") or b""
                snippet = snippet[:256].decode("utf-8", "replace").replace("\n", " ")
            except Exception:
                snippet = ""
            finally:
                resp.close()
            raise SynoError(
                f"download of {remote_path} failed: HTTP {resp.status_code} "
                f"({ctype or '?'})" + (f" — {snippet}" if snippet else "") +
                " — the .part file was kept and will resume on a later run")

        if resume_from and resp.status_code == 206:
            mode, done = "ab", resume_from     # server honors Range -> continue
        else:
            mode, done = "wb", 0               # no Range or Range ignored -> from scratch

        with open(part, mode) as fh:
            for block in resp.iter_content(chunk_size=CHUNK):
                if not block:
                    continue
                fh.write(block)
                done += len(block)
                if on_progress:
                    on_progress(done, size)

        if size and os.path.getsize(part) != size:
            raise IncompleteDownload(
                f"got {os.path.getsize(part)} of {size} bytes")

    # ---- helpers ---------------------------------------------------------

    @staticmethod
    def _finalize(part, meta, local_path, mtime):
        os.replace(part, local_path)           # atomic
        if mtime:
            os.utime(local_path, (mtime, mtime))
        Synology._rm(meta)

    @staticmethod
    def _rm(*paths):
        for p in paths:
            try:
                os.remove(p)
            except FileNotFoundError:
                pass


# ---- multi-line progress renderer ----------------------------------------

def fmt_bar(name, done, total, started, now):
    """One progress-bar line as a string."""
    elapsed = max(now - started, 1e-6)
    speed = done / elapsed / (1 << 20)         # MiB/s
    n = name[:28]
    if total:
        pct = done / total
        filled = int(20 * pct)
        bar = "#" * filled + "-" * (20 - filled)
        return (f"  {n:28} [{bar}] {pct*100:5.1f}%  "
                f"{done/(1<<20):7.1f}/{total/(1<<20):.1f}MiB {speed:6.1f}MiB/s")
    return f"  {n:28} {done/(1<<20):7.1f}MiB {speed:6.1f}MiB/s"


class Progress:
    """Thread-safe multi-line live progress region for N concurrent downloads.

    Reserves `slots` lines at the bottom of the terminal, one bar per worker,
    and repaints them in place with ANSI cursor moves. log() emits a line that
    scrolls ABOVE the live region. On a non-TTY it degrades gracefully: bars are
    dropped and only log() lines are printed.
    """

    def __init__(self, slots):
        self.n = max(1, slots)
        self.lines = [""] * self.n
        self.enabled = sys.stdout.isatty()
        self.lock = threading.Lock()
        self.started = False

    def _width(self):
        try:
            return os.get_terminal_size().columns
        except OSError:
            return 100

    def _reserve(self):
        if not self.started:
            sys.stdout.write("\n" * self.n)    # carve out the region; cursor at bottom
            self.started = True

    def _redraw(self):
        w = self._width()
        sys.stdout.write(f"\033[{self.n}A")    # up to the top of the region
        for line in self.lines:
            sys.stdout.write("\r\033[2K" + line[:w] + "\n")
        sys.stdout.flush()

    def update(self, slot, text):
        if not self.enabled:
            return
        with self.lock:
            self._reserve()
            self.lines[slot] = text
            self._redraw()

    def clear_slot(self, slot):
        if not self.enabled:
            return
        with self.lock:
            self.lines[slot] = ""
            if self.started:
                self._redraw()

    def log(self, msg):
        with self.lock:
            if not self.enabled or not self.started:
                print(msg)
                return
            w = self._width()
            sys.stdout.write(f"\033[{self.n}A")            # to top of region
            sys.stdout.write("\r\033[2K" + msg[:w] + "\n")  # scroll msg above it
            for line in self.lines:                         # repaint region below
                sys.stdout.write("\r\033[2K" + line[:w] + "\n")
            sys.stdout.flush()


class SlotReporter:
    """Bound to one Progress slot; throttles per-chunk updates to ~10/s."""

    def __init__(self, progress, slot, name):
        self.p = progress
        self.slot = slot
        self.name = name
        self.started = time.monotonic()
        self.last = 0.0

    def __call__(self, done, total):
        now = time.monotonic()
        if now - self.last < 0.1 and total and done < total:
            return
        self.last = now
        self.p.update(self.slot, fmt_bar(self.name, done, total, self.started, now))


# ---- parallel download runner --------------------------------------------

STATUS_MARK = {"downloaded": "+", "uploaded": "+", "resumed": "^", "skip": "=",
               "incomplete": "!", "error": "x", "corrupt": "x", "cancelled": "-",
               "toolong": "!"}


class _ProgressReader:
    """File wrapper that reports progress as requests streams it out (uploads)."""

    def __init__(self, fh, size, on_progress):
        self.fh = fh
        self.size = size
        self.on_progress = on_progress
        self.done = 0

    def __len__(self):                           # requests uses this for length
        return self.size

    def read(self, amount=-1):
        block = self.fh.read(amount)
        if block:
            self.done += len(block)
            if self.on_progress:
                self.on_progress(self.done, self.size)
        return block


def _file_md5(path, chunk=CHUNK):
    """Compute the MD5 of a local file (streamed, so it handles big files)."""
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _verify_one(syno, task, status, prog, slot):
    """Post-download MD5 check against the NAS. Returns the (maybe new) status."""
    name = os.path.basename(task["local"])
    prog.update(slot, f"  {name[:28]:28}  verifying MD5 on the NAS …")
    try:
        remote_md5 = syno.md5(task["remote"])
    except SynoError as ex:
        prog.log(f"  ! {task['local']}: MD5 check skipped: {err_text(ex)}")
        return status
    local_md5 = _file_md5(task["local"])
    if remote_md5 and local_md5.lower() != remote_md5.lower():
        prog.log(f"  x {task['local']}: MD5 MISMATCH "
                 f"(local {local_md5}, nas {remote_md5})")
        return "corrupt"
    prog.log(f"  * verified    {task['local']}  ({local_md5})")
    return status


MOVED_STATUSES = ("downloaded", "uploaded", "resumed")
# 'error'/'corrupt' are already logged in detail; 'cancelled' would just spam one
# line per untouched file after a Ctrl-C, so it is only counted in the summary.
QUIET_STATUSES = ("error", "corrupt", "cancelled")

# Statuses collected into the end-of-run failure report. 'toolong' is added by
# cmd_upload (it never enters run_many). Generic reasons for the non-exception ones.
FAILURE_STATUSES = ("error", "corrupt", "incomplete")
FAILURE_REASON = {
    "corrupt": "MD5 did not match the NAS after transfer",
    "incomplete": "incomplete — connection kept dropping (retries exhausted)",
}


def elide_middle(text, maxlen):
    """Shorten `text` to `maxlen` chars keeping both ends (…in the middle…)."""
    if maxlen <= 0:
        return ""
    if len(text) <= maxlen:
        return text
    if maxlen == 1:
        return "…"
    keep = maxlen - 1                        # room for the ellipsis
    tail = keep // 2
    head = keep - tail
    return text[:head] + "…" + (text[-tail:] if tail else "")


def run_many(tasks, work, jobs, label_of, path_of=None):
    """Run `work(task, reporter, log_fn, prog, slot) -> status` over `tasks`.

    Shared by download and upload so both get the same concurrency, live progress
    bars and accounting. Returns (stats, total_bytes, failures); a task's "size"
    counts toward the byte total whenever data actually moved.

    `path_of(task)`, if given, supplies the destination path shown in brackets
    after the filename on each completion line, elided to fit the terminal width.

    `failures` is a list of (status, path, reason) tuples for every error /
    corrupt / incomplete item, so print_summary() can list them at the END —
    otherwise a single failure in a multi-hour run scrolls off the screen forever.
    """
    stats, total = new_stats(), 0
    failures = []
    if not tasks:
        return stats, total, failures

    workers = max(1, min(jobs, len(tasks)))
    prog = Progress(workers)
    stat_lock = threading.Lock()
    free = list(range(workers))                 # available render slots
    free_lock = threading.Lock()

    def run(task):
        nonlocal total
        if interrupted():                # clean stop: start nothing new
            with stat_lock:
                stats["cancelled"] = stats.get("cancelled", 0) + 1
            return "cancelled"
        with free_lock:
            slot = free.pop()
        reason = None
        try:
            reporter = SlotReporter(prog, slot, label_of(task))
            try:
                status = work(task, reporter, prog.log, prog, slot)
            except SynoError as ex:
                status, reason = "error", err_text(ex)
                prog.log(f"  x {label_of(task)}: {reason}")
            except requests.RequestException as ex:
                status, reason = "error", str(ex)
                prog.log(f"  x {label_of(task)}: {ex}")
            except OSError as ex:
                status, reason = "error", str(ex)
                prog.log(f"  x {label_of(task)}: {ex}")
        finally:
            prog.clear_slot(slot)
            with free_lock:
                free.append(slot)
        if status in FAILURE_STATUSES:
            where = path_of(task) if path_of else label_of(task)
            with stat_lock:
                failures.append((status, where, reason or FAILURE_REASON.get(status, "")))
        with stat_lock:
            stats[status] = stats.get(status, 0) + 1
            if status in MOVED_STATUSES:
                total += task["size"]
        if status not in QUIET_STATUSES:
            line = f"  {STATUS_MARK.get(status, '?')} {status:10} {label_of(task)}"
            if path_of:
                dest = path_of(task)
                if prog.enabled:             # fit the whole line to the terminal
                    dest = elide_middle(dest, prog._width() - len(line) - 4)
                if dest:
                    line += f"  [{dest}]"
            prog.log(line)
        return status

    if workers == 1:
        for task in tasks:
            run(task)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(run, tasks))
    return stats, total, failures


def _download_many(syno, tasks, jobs, retries, verify=False, by_checksum=False):
    """Download tasks {remote,size,mtime,local} with up to `jobs` workers."""
    def work(task, reporter, log_fn, prog, slot):
        status = syno.download_file(task["remote"], task["size"], task["mtime"],
                                    task["local"], retries=retries,
                                    on_progress=reporter, log=log_fn,
                                    by_checksum=by_checksum)
        if verify and status in ("downloaded", "resumed"):
            status = _verify_one(syno, task, status, prog, slot)
        return status
    return run_many(tasks, work, jobs, lambda t: os.path.basename(t["local"]),
                    path_of=lambda t: t["local"])


def _upload_many(syno, tasks, jobs, retries, verify=False, by_checksum=False):
    """Upload tasks {local,remote_dir,name,size,mtime,remote_entry} with workers."""
    def work(task, reporter, log_fn, prog, slot):
        status = syno.upload_file(task["local"], task["remote_dir"], task["size"],
                                  task["mtime"], name=task["name"],
                                  retries=retries, on_progress=reporter,
                                  log=log_fn, remote_entry=task.get("remote_entry"),
                                  by_checksum=by_checksum)
        if verify and status == "uploaded":
            status = _verify_upload(syno, task, status, prog, slot)
        return status
    return run_many(tasks, work, jobs, lambda t: os.path.basename(t["local"]),
                    path_of=lambda t: clean_remote(f"{t['remote_dir']}/{t['name']}"))


def _verify_upload(syno, task, status, prog, slot):
    """Post-upload MD5 check: compare the local file against the NAS copy."""
    remote = f"{clean_remote(task['remote_dir'])}/{task['name']}"
    prog.update(slot, f"  {task['name'][:28]:28}  verifying MD5 on the NAS …")
    info = syno.stat(remote)
    if info is None:
        prog.log(f"  x {task['local']}: verify failed (not on the NAS after upload)")
        return "corrupt"
    if info["size"] != task["size"]:
        prog.log(f"  x {task['local']}: SIZE MISMATCH "
                 f"(local {task['size']}, nas {info['size']})")
        return "corrupt"
    try:
        remote_md5 = syno.md5(remote)
    except SynoError as ex:
        prog.log(f"  ! {task['local']}: MD5 check skipped: {err_text(ex)}")
        return status
    local_md5 = _file_md5(task["local"])
    if remote_md5 and local_md5.lower() != remote_md5.lower():
        prog.log(f"  x {task['local']}: MD5 MISMATCH "
                 f"(local {local_md5}, nas {remote_md5})")
        return "corrupt"
    prog.log(f"  * verified    {task['local']}  ({local_md5})")
    return status


# ---- summary helper ------------------------------------------------------

STATUS_ORDER = ("downloaded", "uploaded", "resumed", "skip", "incomplete",
                "deleted", "cancelled", "toolong", "corrupt", "error")


def new_stats():
    return {k: 0 for k in STATUS_ORDER}


def print_summary(stats, total_bytes, verb="downloaded/resumed", failures=None):
    """Print the end-of-run summary. RETURNS the process exit code: 1 if anything
    failed (error/corrupt/incomplete/toolong) so cron and scripts can detect it,
    0 when the run was clean (skips and deletes are not failures)."""
    print("\n===== Summary =====")
    for k in STATUS_ORDER:
        print(f"  {k:12}: {stats.get(k, 0)}")
    print(f"  {'bytes':12}: {human(total_bytes)} {verb}")
    if stats.get("incomplete"):
        print("  (incomplete downloads: run the same command again to resume them)")
    if stats.get("corrupt"):
        print("  (corrupt: MD5 did not match the NAS — re-run to download again)")
    if stats.get("cancelled"):
        print("  (cancelled by Ctrl-C: re-run the same command to pick up where "
              "it stopped)")
    if stats.get("toolong"):
        print("  (toolong: filename too long for the NAS — re-run with "
              "--truncate-names to upload them under a shortened name)")
    print_failures(failures or [])
    return 1 if any(stats.get(k) for k in
                    ("error", "corrupt", "incomplete", "toolong")) else 0


def print_failures(failures, limit=100):
    """List every failed item at the END of the run, so nothing scrolls away."""
    if not failures:
        return
    print(f"\n----- {len(failures)} failure(s) — re-run to retry them -----")
    for status, where, reason in failures[:limit]:
        print(f"  {STATUS_MARK.get(status, 'x')} {status:10} {where}")
        if reason:
            print(f"       ↳ {reason}")
    if len(failures) > limit:
        print(f"  … and {len(failures) - limit} more (see the scrollback above)")


# ---- commands ------------------------------------------------------------

def print_json(obj):
    """Pretty-print a dict as stable, UTF-8-friendly JSON."""
    print(json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False))


def _resolve_or_exit(host):
    """Resolve a QuickConnect ID to its coordinator info, or exit with a message.

    Shared by 'resolve' and 'qcinfo': one place for the network error and the
    'no such ID' (errno) handling. Returns (server_id, info).
    """
    server_id = qc_extract_id(host)
    log.info("Querying QuickConnect coordinator for ID '%s' ...", server_id)
    try:
        info = qc_resolve(server_id)
    except (requests.RequestException, SynoError) as e:
        sys.exit(f"error: could not query the QuickConnect coordinator: {err_text(e)}")
    err = qc_error(info)
    if err:
        sys.exit(f"error: {err}\n"
                 f"       (no record for ID '{server_id}' — double-check the "
                 f"QuickConnect ID, and that the NAS is set up and online)")
    return server_id, info


def cmd_resolve(host):
    """Diagnose QuickConnect: list direct endpoints and which respond.

    Runs before login and needs no credentials — just the QuickConnect host/ID.
    """
    _server_id, info = _resolve_or_exit(host)
    cands = qc_candidates(info)
    if not cands:
        print("No direct endpoints advertised. The NAS is probably relay-only "
              "(no LAN reachability, no port-forwarding, no DDNS).")
        return
    requests.packages.urllib3.disable_warnings()  # type: ignore # raw-IP probes
    print(f"{'STATUS':7} {'KIND':16} URL")
    first = None
    for label, url, verify in cands:
        ok = qc_probe(url, verify)
        if ok and first is None:
            first = (url, verify, label)
        tls = "" if verify else "  (TLS not verified: raw IP)"
        print(f"{'OK' if ok else 'no':7} {label:16} {url}{tls}")
    if first:
        url, verify, label = first
        print(f"\nDirect path available via '{label}': {url}")
        print(f"Use it with:  --direct   (or set SYNOLOGY_HOST={url}"
              f"{' plus --insecure' if not verify else ''})")
    else:
        print("\nNo direct endpoint responded — you'll have to use the relay "
              "(just the plain quickconnect.to host, without --direct).")


def cmd_qcinfo(host, as_json=False):
    """Dump everything the QuickConnect coordinator exposes about the NAS.

    Pre-login, NO credentials: this is exactly the data anyone can obtain from
    just the QuickConnect ID, which makes it a handy privacy audit. (`info` is the
    logged-in counterpart that talks to the DSM API itself.)
    """
    server_id, info = _resolve_or_exit(host)
    if as_json:
        print_json(info)
        return

    server = info.get("server") or {}
    service = info.get("service") or {}
    smart = info.get("smartdns") or {}
    env = info.get("env") or {}

    def row(k, v):
        print(f"  {k:15} : {v}")

    print("===== identity =====")
    row("quickconnect id", server_id)
    row("server id", server.get("serverID", "?"))
    row("ds state", server.get("ds_state", "?"))
    row("relay region", env.get("relay_region", "?"))
    row("answered by", env.get("control_host", QC_COORDINATOR))

    print("\n===== addresses (exposed WITHOUT authentication) =====")
    ext = server.get("external") or {}
    if ext.get("ip"):
        row("public/WAN IPv4", ext["ip"])
    if ext.get("ipv6") and ext["ipv6"] not in ("::", ""):
        row("public/WAN IPv6", ext["ipv6"])
    for itf in server.get("interface") or []:
        ipv6 = ", ".join(a.get("address", "") for a in itf.get("ipv6") or [])
        val = f"{itf.get('ip', '?')}/{itf.get('mask', '?')} ({itf.get('name', 'if')})"
        row("LAN interface", val + (f"  ipv6: {ipv6}" if ipv6 else ""))
    if server.get("gateway"):
        row("LAN gateway", server["gateway"])
    for key in ("ddns", "fqdn"):
        val = server.get(key)
        if val and val not in ("NULL", ""):
            row(key, val)

    print("\n===== smartdns (public DNS names resolving to the IPs above) =====")
    if smart.get("host"):
        row("host", smart["host"])
    for fqdn in as_list(smart.get("lan")):
        row("lan", fqdn)
    for fqdn in as_list(smart.get("external")):
        row("external", fqdn)

    print("\n===== service / relay =====")
    row("dsm port", f"{service.get('port', '?')}   ext_port: {service.get('ext_port', '?')}")
    if service.get("relay_dn"):
        row("relay", f"{service['relay_dn']}:{service.get('relay_port', '?')}"
                     f"  (ip {service.get('relay_ip', '?')})")
    if service.get("https_ip"):
        row("relay https", f"{service['https_ip']}:{service.get('https_port', '?')}")

    print("\nNOTE: everything above is returned by Synology's coordinator to anyone")
    print("who knows the QuickConnect ID — no login needed. To avoid exposing your")
    print("public/LAN IPs, use a VPN instead of QuickConnect (see README > Privacy).")


def _fmt_time(ts):
    """Format a UNIX timestamp as a local date-time string (or '-' if empty)."""
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return "-"
    if not ts:
        return "-"
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def fmt_entry(e):
    """One `ls`-style line for a file entry dict: type flag, size, path."""
    flag = "d" if e["isdir"] else "-"
    size = "" if e["isdir"] else human(e["size"])
    return f"{flag} {size:>10}  {e['path']}"


def cmd_ping(syno, _args):
    """Connectivity + auth check: report the endpoint, session and DSM identity.

    By the time this runs we have already connected and logged in (main() does
    that for every command), so reaching here means the round trip works — we
    just print what we are actually talking to.
    """
    print(f"endpoint     : {syno.base}")
    print(f"tls verified : {syno.verify}")
    print(f"logged in as : {syno.account}")
    t0 = time.perf_counter()
    try:
        info = syno.server_info()
        dt = (time.perf_counter() - t0) * 1000
        print(f"round trip   : {dt:.0f} ms (SYNO.FileStation.Info)")
        for key in ("hostname", "is_manager", "support_sharing",
                    "support_virtual_protocol", "system_codepage"):
            if key in info:
                print(f"{key:13}: {info[key]}")
    except SynoError as e:
        print(f"info         : not available ({err_text(e)})")
    have = sorted(n for n in syno.OPTIONAL_APIS if n in syno.apis)
    miss = sorted(n for n in syno.OPTIONAL_APIS if n not in syno.apis)
    print(f"optional apis: {', '.join(have) or 'none'}")
    if miss:
        print(f"  (missing   : {', '.join(miss)})")
    print("OK: connection and login are working.")


def cmd_info(syno, args):
    """Dump everything we can read about the NAS via the (non-admin) API."""
    data = syno.gather_info()
    if args.json:
        print_json(data)
        return

    print("===== connection =====")
    for k, v in data["connection"].items():
        print(f"  {k:16}: {v}")

    fs = data.get("filestation_info") or {}
    print("\n===== file station =====")
    if "error" in fs and len(fs) == 1:
        print(f"  (unavailable: {fs['error']})")
    else:
        for k in sorted(fs):
            print(f"  {k:24}: {fs[k]}")

    apis = data.get("apis") or {}
    print("\n===== api catalog =====")
    if "error" in apis and len(apis) == 1:
        print(f"  (unavailable: {apis['error']})")
    else:
        print(f"  {len(apis)} APIs advertised by this DSM:\n")
        for name in sorted(apis):
            meta = apis[name]
            print(f"  {name:42} v{meta.get('minVersion','?')}-{meta.get('maxVersion','?')}"
                  f"  ({meta.get('path','?')})")
    print("\n(tip: 'info --json' dumps the same data machine-readable; most "
          "SYNO.Core.* system APIs need an admin account and are not shown.)")


def cmd_stat(syno, args):
    """Show detailed metadata for a single remote path."""
    f = syno.getinfo(args.remote)
    if f is None:
        raise SynoError(f"remote path not found: {args.remote}")
    add = f.get("additional", {})
    print(f"path   : {f.get('path')}")
    print(f"name   : {f.get('name')}")
    print(f"type   : {'directory' if f.get('isdir') else 'file'}")
    if "real_path" in add:
        print(f"real   : {add['real_path']}")
    if not f.get("isdir") and "size" in add:
        print(f"size   : {human(add['size'])}  ({add['size']} bytes)")
    t = add.get("time", {})
    if t:
        print(f"mtime  : {_fmt_time(t.get('mtime'))}  (modified)")
        print(f"ctime  : {_fmt_time(t.get('ctime'))}  (metadata change)")
        print(f"atime  : {_fmt_time(t.get('atime'))}  (accessed)")
        print(f"crtime : {_fmt_time(t.get('crtime'))}  (created)")
    owner = add.get("owner", {})
    if owner:
        print(f"owner  : {owner.get('user','?')}:{owner.get('group','?')} "
              f"(uid {owner.get('uid','?')}, gid {owner.get('gid','?')})")
    perm = add.get("perm", {})
    if isinstance(perm, dict) and "posix" in perm:
        print(f"perm   : {perm['posix']:o}  (posix)")


def cmd_md5(syno, args):
    """Print the NAS-computed MD5 checksum of a remote file."""
    info = syno.stat(args.remote)
    if info is None:
        raise SynoError(f"remote path not found: {args.remote}")
    if info["isdir"]:
        raise SynoError(f"md5 needs a file, not a directory: {args.remote}")
    digest = syno.md5(args.remote)
    if not digest:
        raise SynoError(f"the NAS finished the MD5 task but returned no digest "
                        f"for: {args.remote}")
    print(f"{digest}  {args.remote}")


def cmd_search(syno, args):
    """Recursively search the NAS for files/dirs matching a name pattern."""
    # The Search API needs a real folder; "/" is not one. With no path (or "/")
    # search every accessible share and merge the results.
    if args.path in (None, "", "/"):
        roots = syno.list_shares()
        where = "all shares"
    else:
        roots = [args.path]
        where = args.path
    results = []
    for root in roots:
        results.extend(syno.search(root, args.pattern))
    if not results:
        print(f"no matches for '{args.pattern}' under {where}")
        return
    for e in sorted(results, key=lambda x: (not x["isdir"], x["path"])):
        print(fmt_entry(e))
    print(f"\n{len(results)} match(es).")


def cmd_shares(syno, _args):
    for path in syno.list_shares():
        print(path)


def cmd_ls(syno, args):
    path = args.path
    if not path or path == "/":
        for p in syno.list_shares():
            print(f"d {'':>10}  {p}")
        return
    for e in sorted(syno.listdir(path), key=lambda x: (not x["isdir"], x["name"])):
        print(fmt_entry(e))


def cmd_du(syno, args):
    info = syno.stat(args.remote)
    if info is None:
        raise SynoError(f"remote path not found: {args.remote}")
    if not info["isdir"]:                       # a single file: just its size
        print(f"{human(info['size']):>10}  {args.remote}")
        return

    root = clean_remote(args.remote)
    dir_sizes, file_sizes, grand = {}, {}, 0
    for e in syno.walk(root):                    # one recursive pass, sum into ancestors
        grand += e["size"]
        parts = os.path.relpath(e["path"], root).split("/")
        for k in range(len(parts)):              # root, root/a, root/a/b ...
            d = root if k == 0 else root + "/" + "/".join(parts[:k])
            dir_sizes[d] = dir_sizes.get(d, 0) + e["size"]
        if args.all:
            file_sizes[e["path"]] = e["size"]

    def depth(p):
        return 0 if p == root else len(os.path.relpath(p, root).split("/"))

    rows = [(p, s, True) for p, s in dir_sizes.items() if 1 <= depth(p) <= args.max_depth]
    if args.all:
        rows += [(p, s, False) for p, s in file_sizes.items() if depth(p) <= args.max_depth]
    for p, s, isdir in sorted(rows):
        print(f"{human(s):>10}  {p}{'/' if isdir else ''}")
    print(f"{human(grand):>10}  {root}  (TOTAL)")


def _sync_dir(syno, remote, dest, delete=False, retries=5, jobs=1, verify=False,
              by_checksum=False):
    """One-way sync (remote -> local) of a remote directory into dest (rsync-like).

    Never writes to the NAS. The full remote directory tree is recreated locally,
    including EMPTY directories. With delete=True it also removes local files that
    no longer exist on the remote, turning it into a one-way mirror — but only if
    the remote walk completed: mirroring against a list truncated by Ctrl-C would
    delete files that were never even looked at. Files are downloaded with up to
    `jobs` concurrent workers. Returns the summary's exit code (0 clean, 1 fails).
    """
    contents_only = remote.endswith("/")
    remote_dir = remote.rstrip("/")
    target = dest if contents_only else os.path.join(dest, os.path.basename(remote_dir))

    tasks, expected, dirs = [], set(), []        # build the work list from one walk
    for e in syno.walk(remote_dir, dirs=dirs):
        rel = os.path.relpath(e["path"], remote_dir)
        local_path = os.path.join(target, rel)
        expected.add(os.path.abspath(local_path))
        tasks.append({"remote": e["path"], "size": e["size"],
                      "mtime": e["mtime"], "local": local_path})

    # Recreate the remote directory tree first: this materializes EMPTY dirs
    # (which carry no files of their own) and marks them expected, so --delete
    # does not prune directories that do exist on the remote.
    for d in dirs:
        p = os.path.join(target, os.path.relpath(d["path"], remote_dir))
        expected.add(os.path.abspath(p))
        try:
            os.makedirs(p, exist_ok=True)
        except OSError as e:
            log.warning("could not create local folder %s: %s", p, e)

    stats, total_bytes, failures = _download_many(syno, tasks, jobs, retries,
                                                  verify=verify,
                                                  by_checksum=by_checksum)
    if delete:
        if interrupted():
            log.warning("--delete SKIPPED: the remote walk was interrupted, so "
                        "the list of remote files is INCOMPLETE — mirroring now "
                        "could delete local files that were never checked")
        else:
            stats["deleted"] = _mirror_delete(target, expected)
    return print_summary(stats, total_bytes, failures=failures)


def _mirror_delete(target, expected):
    """Remove local files under target not present in `expected`. Returns count.

    Leaves transient .part/.part.json sidecars alone (they belong to in-flight
    or resumable downloads) and prunes directories that become empty.
    """
    if not os.path.isdir(target):
        return 0
    deleted = 0
    for root, _dirs, files in os.walk(target, topdown=False):
        for name in files:
            if interrupted():
                log.warning("--delete: interrupted — stopping the local mirror "
                            "pass with %d file(s) deleted; re-run the same "
                            "command to finish it", deleted)
                return deleted
            if name.endswith((".part", ".part.json")):
                continue
            p = os.path.join(root, name)
            if os.path.abspath(p) not in expected:
                print(f"  - {p} (removed: not on remote)")
                os.remove(p)
                deleted += 1
        # prune now-empty directories (never the target root itself, and never
        # one that mirrors an EMPTY directory that exists on the remote)
        if (os.path.abspath(root) != os.path.abspath(target)
                and os.path.abspath(root) not in expected
                and not os.listdir(root)):
            os.rmdir(root)
    return deleted


def _finish_partials(syno, scan_dir, retries, jobs=1, verify=False):
    """Retry ONLY the half-downloaded files under scan_dir.

    Finds every '*.part.json' sidecar, reads the remote path + signature it
    recorded, and resumes just those files — no remote directory walk needed.
    Resumes with up to `jobs` concurrent workers.
    """
    sidecars = []
    if os.path.isfile(scan_dir) and scan_dir.endswith(".part.json"):
        sidecars = [scan_dir]                  # a specific sidecar was named
    else:
        base = scan_dir if os.path.isdir(scan_dir) else os.path.dirname(scan_dir) or "."
        for root, _dirs, files in os.walk(base):
            sidecars += [os.path.join(root, f) for f in files if f.endswith(".part.json")]

    if not sidecars:
        print(f"No partial downloads (*.part.json) found under {scan_dir}")
        return 0

    tasks = []
    for meta in sorted(sidecars):
        try:
            with open(meta) as fh:
                m = json.load(fh)
        except Exception:
            print(f"  ! {meta}: unreadable sidecar, skipping")
            continue
        if not isinstance(m, dict):
            # e.g. a real downloaded file that happens to be named *.part.json
            print(f"  ! {meta}: not a synofs sidecar (unexpected JSON), skipping")
            continue
        remote = m.get("remote")
        size, mtime = m.get("size"), m.get("mtime")
        final = meta[:-len(".part.json")]      # 'X.part.json' -> 'X'
        if not remote or not isinstance(size, int) or not isinstance(mtime, int):
            print(f"  ! {meta}: incomplete sidecar (no remote/size/mtime recorded) "
                  f"— re-download '{os.path.basename(final)}' with 'download' "
                  f"instead")
            continue
        tasks.append({"remote": remote, "size": size,
                      "mtime": mtime, "local": final})

    if not tasks:
        return 0
    print(f"-> Resuming {len(tasks)} partial download(s)")
    stats, total_bytes, failures = _download_many(syno, tasks, jobs, retries,
                                                  verify=verify)
    return print_summary(stats, total_bytes, failures=failures)


def cmd_download(syno, args):
    if args.resume_only:                       # just finish local partials
        # Scan dir = the bare positional if given (`download --resume-only
        # ./somedir`), else dest (`sync --resume-only` with no positional).
        return _finish_partials(syno, args.remote or args.dest, args.retries,
                                jobs=args.jobs, verify=args.verify)
    if not args.remote:
        raise SynoError("download needs a REMOTE path (or use --resume-only)")
    info = syno.stat(args.remote)
    if info is None:
        raise SynoError(f"remote path not found: {args.remote}")
    if info["isdir"]:
        return _sync_dir(syno, args.remote, args.dest, retries=args.retries,
                         jobs=args.jobs, verify=args.verify,
                         by_checksum=args.checksum)
    dest = args.dest
    if dest.endswith(("/", os.sep)) or os.path.isdir(dest):
        dest = os.path.join(dest, info["name"])
    task = {"remote": args.remote, "size": info["size"],
            "mtime": info["mtime"], "local": dest}
    stats, total_bytes, failures = _download_many(syno, [task], args.jobs,
                                                 args.retries, verify=args.verify,
                                                 by_checksum=args.checksum)
    return print_summary(stats, total_bytes, failures=failures)


def cmd_mkdir(syno, args):
    """Create a folder on the NAS (mkdir(1) semantics; -p creates parents)."""
    path = clean_remote(args.remote)
    if path == "/":
        raise SynoError("refusing to create the root folder '/'")
    existing = syno.stat(path)
    if existing is not None:
        if not existing["isdir"]:
            raise SynoError(f"path exists and is a file: {path}")
        if not args.parents:
            raise SynoError(f"folder already exists: {path} (use -p to ignore)")
        print(f"already exists: {path}")
        return
    syno.mkdir(path, force_parent=args.parents)
    print(f"created: {path}")


def _remote_index(syno, remote_dir):
    """Map remote path -> entry for every file under remote_dir (empty if absent)."""
    info = syno.stat(remote_dir)
    if info is None or not info["isdir"]:
        return {}
    return {e["path"]: e for e in syno.walk(remote_dir)}


def _remote_paths(target_root, rel):
    """The (remote_dir, remote_path) a local relative path maps to."""
    sub, _, leaf = rel.rpartition("/")
    rdir = clean_remote(f"{target_root}/{sub}") if sub else target_root
    return rdir, clean_remote(f"{rdir}/{leaf}")


def cmd_upload(syno, args):
    """Recursively upload a local file or directory TO the NAS.

    This is the one command that writes to the NAS. Path semantics follow the
    same trailing-slash rule as sync, applied to the LOCAL side:
        upload ./dir  /share  ->  /share/dir/...   (creates the folder)
        upload ./dir/ /share  ->  /share/...       (contents only)
    """
    local = args.local
    if not os.path.exists(local):
        raise SynoError(f"local path not found: {local}")
    remote_root = clean_remote(args.remote)
    max_bytes = args.max_name_bytes
    too_long = []                                # (local_path, rel) skipped names
    stat_errors = []                             # (local_path, reason) unreadable

    # --- a single file ---------------------------------------------------
    if os.path.isfile(local):
        st = os.stat(local)
        name = os.path.basename(local)
        safe = fit_name(name, max_bytes) if args.truncate_names else name
        if not args.truncate_names and name_bytes(name) > max_bytes:
            _report_too_long([(local, name)], max_bytes)
            return print_summary({"toolong": 1}, 0, "uploaded", [
                ("toolong", local,
                 f"name too long ({name_bytes(name)} bytes > {max_bytes})")])
        if safe != name:
            log.warning("name too long, uploading as '%s'", safe)
        task = {"local": local, "remote_dir": remote_root, "name": safe,
                "size": st.st_size, "mtime": int(st.st_mtime),
                "remote_entry": syno.stat(f"{remote_root}/{safe}")}
        stats, total, failures = _upload_many(syno, [task], args.jobs, args.retries,
                                             verify=args.verify,
                                             by_checksum=args.checksum)
        return print_summary(stats, total, "uploaded", failures)

    # --- a directory tree ------------------------------------------------
    contents_only = local.endswith(("/", os.sep))
    local_dir = local.rstrip("/" + os.sep) or local
    target_root = (remote_root if contents_only
                   else clean_remote(f"{remote_root}/{os.path.basename(local_dir)}"))

    index = _remote_index(syno, target_root)     # one walk: powers skip + --delete
    tasks, expected = [], set()
    symlinks = []                                # skipped symlinks (files + dirs)
    for dirpath, dirnames, files in os.walk(local_dir):
        if interrupted():
            break
        for d in dirnames:                       # os.walk never follows dir symlinks
            dp = os.path.join(dirpath, d)
            if os.path.islink(dp):
                symlinks.append(dp)
        for name in sorted(files):
            lp = os.path.join(dirpath, name)
            if name.endswith((".part", ".part.json")):
                continue
            if os.path.islink(lp):
                symlinks.append(lp)
                continue
            rel_dir = os.path.relpath(dirpath, local_dir).replace(os.sep, "/")
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            # A too-long segment (file OR directory) would fail on the NAS FS.
            if rel_has_long_segment(rel, max_bytes):
                if args.truncate_names:
                    rel = fit_rel_path(rel, max_bytes)
                else:
                    too_long.append((lp, rel))
                    # The file EXISTS locally, so --delete must never remove its
                    # NAS counterpart: protect both the raw path and the
                    # truncated one (an earlier --truncate-names run may have
                    # uploaded it under the shortened name).
                    expected.add(_remote_paths(target_root, rel)[1])
                    expected.add(_remote_paths(
                        target_root, fit_rel_path(rel, max_bytes))[1])
                    continue
            rdir, rpath = _remote_paths(target_root, rel)
            leaf = os.path.basename(rpath)
            expected.add(rpath)
            try:
                st = os.stat(lp)
            except OSError as e:
                log.warning("skipping %s: %s", lp, e)
                stat_errors.append((lp, str(e)))
                continue
            tasks.append({"local": lp, "remote_dir": rdir, "name": leaf,
                          "size": st.st_size, "mtime": int(st.st_mtime),
                          "remote_entry": index.get(rpath)})

    if too_long:
        _report_too_long(too_long, max_bytes)
    if symlinks:
        shown = ", ".join(symlinks[:5]) + (" …" if len(symlinks) > 5 else "")
        log.info("%d symlink(s) SKIPPED (upload copies regular files only; like "
                 "rsync without -l, symlinked dirs are not followed): %s",
                 len(symlinks), shown)
    if not index and not interrupted():          # target absent: create it once
        syno.mkdir(target_root, force_parent=True)
    stats, total, failures = _upload_many(syno, tasks, args.jobs, args.retries,
                                         verify=args.verify,
                                         by_checksum=args.checksum)
    # Fold the planning-phase problems (skipped before run_many even ran) into
    # the counts and the end-of-run failure report.
    stats["toolong"] = len(too_long)
    stats["error"] = stats.get("error", 0) + len(stat_errors)
    for lp, rel in too_long:
        worst = max((name_bytes(s) for s in rel.split("/")), default=0)
        failures.append(("toolong", lp, f"name too long ({worst} bytes > {max_bytes})"))
    for lp, reason in stat_errors:
        failures.append(("error", lp, f"could not read local file: {reason}"))
    if args.delete:
        if interrupted():
            log.warning("--delete SKIPPED: the run was interrupted, so the list "
                        "of local files is INCOMPLETE — mirroring now could "
                        "delete NAS files that were never checked")
        else:
            n_del, del_errors = _remote_mirror_delete(syno, index, expected,
                                                      args.yes)
            stats["deleted"] = n_del
            if del_errors:                 # failed deletes are run failures
                stats["error"] = stats.get("error", 0) + len(del_errors)
                failures.extend(("error", p, r) for p, r in del_errors)
    return print_summary(stats, total, "uploaded", failures)


def _report_too_long(items, max_bytes):
    """Warn clearly about names skipped for exceeding the NAS filename byte limit."""
    log.warning("%d file(s) skipped: name too long for the NAS (> %d bytes). "
                "Re-run with --truncate-names to upload them under a shortened "
                "name:", len(items), max_bytes)
    for lp, rel in items[:20]:
        worst = max((name_bytes(seg) for seg in rel.split("/")), default=0)
        print(f"  ! {lp}  (segment {worst} bytes)", file=sys.stderr)
    if len(items) > 20:
        print(f"  … and {len(items) - 20} more", file=sys.stderr)


def _remote_mirror_delete(syno, index, expected, assume_yes):
    """Delete NAS files not present locally. Destructive — hence the confirmation.

    Returns (deleted_count, errors): errors is a list of (path, reason) for
    deletes the NAS rejected, so the caller can fold them into the summary and
    the exit code — a mirror pass that could not delete everything is NOT a
    clean run. Stops issuing new deletes once Ctrl-C was pressed.
    """
    doomed = sorted(p for p in index if p not in expected)
    if not doomed:
        return 0, []
    print(f"\n--delete: {len(doomed)} file(s) on the NAS are not present locally:")
    for p in doomed[:20]:
        print(f"  - {p}")
    if len(doomed) > 20:
        print(f"  … and {len(doomed) - 20} more")
    if not assume_yes:
        try:
            answer = input(f"Delete these {len(doomed)} file(s) from the NAS? [y/N] ")
        except EOFError:
            answer = ""            # non-interactive stdin: default to NO (safe)
        if answer.strip().lower() not in ("y", "yes"):
            print("Aborted: nothing was deleted (pass --yes to delete without "
                  "prompting).")
            return 0, []
    deleted, errors = 0, []
    for p in doomed:
        if interrupted():
            log.warning("--delete: interrupted — %d file(s) left on the NAS; "
                        "re-run the same command to finish the mirror pass",
                        len(doomed) - deleted - len(errors))
            break
        try:
            syno.delete(p)
            deleted += 1
        except SynoError as e:
            log.warning("could not delete %s: %s", p, err_text(e))
            errors.append((p, err_text(e)))
    return deleted, errors


def cmd_sync(syno, args):
    if args.resume_only:                       # just finish local partials
        # Scan dir = the bare positional if given (`download --resume-only
        # ./somedir`), else dest (`sync --resume-only` with no positional).
        return _finish_partials(syno, args.remote or args.dest, args.retries,
                                jobs=args.jobs, verify=args.verify)
    if not args.remote:
        raise SynoError("sync needs a REMOTE path (or use --resume-only)")
    info = syno.stat(args.remote.rstrip("/"))
    if info is None:
        raise SynoError(f"remote path not found: {args.remote}")
    if not info["isdir"]:
        raise SynoError(f"sync target is not a directory: {args.remote} "
                        f"(use 'download' for a single file)")
    return _sync_dir(syno, args.remote, args.dest, delete=args.delete,
                     retries=args.retries, jobs=args.jobs, verify=args.verify,
                     by_checksum=args.checksum)


# ---- entrypoint ----------------------------------------------------------

CHECKSUM_HELP = ("decide what to transfer by MD5 instead of size+mtime: compare "
                 "every file's checksum (NAS vs local) and transfer only the ones "
                 "that differ. Costs one md5 task on the NAS per already-present "
                 "file, so it is much slower than the default on big trees.")


def build_parser():
    # Global options live on a shared parent so they are accepted BOTH before and
    # after the subcommand (e.g. both `synofs.py --host X resolve` and
    # `synofs.py resolve --host X` work). SUPPRESS keeps an unset flag from
    # clobbering a value given on the other side; set_defaults() sets the baseline.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--host", default=argparse.SUPPRESS,
                        help="NAS URL (overrides $SYNOLOGY_HOST)")
    common.add_argument("--user", default=argparse.SUPPRESS,
                        help="username (overrides $SYNOLOGY_USER)")
    common.add_argument("--insecure", action="store_true", default=argparse.SUPPRESS,
                        help="do not verify the TLS certificate (self-signed NAS cert)")
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
                        help="verbose (DEBUG) logging: show every API call")
    common.add_argument("-q", "--quiet", action="store_true", default=argparse.SUPPRESS,
                        help="quiet: only warnings and errors on stderr")
    common.add_argument("--retries", type=int, default=argparse.SUPPRESS,
                        help="times to retry+resume a file after a drop (default 5)")
    common.add_argument("--direct", action="store_true", default=argparse.SUPPRESS,
                        help="try to bypass the QuickConnect relay: resolve the NAS's "
                             "own LAN/WAN/DDNS address and connect to it directly")

    ap = argparse.ArgumentParser(
        parents=[common],
        description="Synology File Station CLI over the DSM Web API "
                    "(QuickConnect-friendly, with resume and verification).")
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("shares", parents=[common],
                   help="list accessible shared folders")

    sub.add_parser("resolve", parents=[common],
                   help="show QuickConnect direct endpoints and which "
                        "respond (diagnose --direct; no login needed)")

    p_qcinfo = sub.add_parser("qcinfo", parents=[common],
                              help="dump what QuickConnect exposes about the NAS "
                                   "(pre-login, no credentials; privacy audit)")
    p_qcinfo.add_argument("--json", action="store_true",
                          help="dump the raw coordinator response as JSON")

    sub.add_parser("ping", parents=[common],
                   help="check connectivity + login and show the DSM identity")

    p_info = sub.add_parser("info", parents=[common],
                            help="dump everything the API exposes about the NAS")
    p_info.add_argument("--json", action="store_true",
                        help="dump the collected info as JSON instead of text")

    p_stat = sub.add_parser("stat", parents=[common],
                            help="show detailed metadata for a remote path")
    p_stat.add_argument("remote", help="remote file or directory path")

    p_md5 = sub.add_parser("md5", parents=[common],
                           help="print the NAS-computed MD5 of a remote file")
    p_md5.add_argument("remote", help="remote file path")

    p_search = sub.add_parser("search", parents=[common],
                              help="recursively search the NAS by name pattern")
    p_search.add_argument("pattern", help="name pattern to match (e.g. '*.pdf')")
    p_search.add_argument("path", nargs="?", default="/",
                          help="remote directory to search under (default: all shares)")

    p_ls = sub.add_parser("ls", parents=[common], help="list a remote directory")
    p_ls.add_argument("path", nargs="?", default=None,
                      help="remote path (default: list shares)")

    p_du = sub.add_parser("du", parents=[common],
                          help="show disk usage of a file or directory")
    p_du.add_argument("remote", help="remote file or directory path")
    p_du.add_argument("-d", "--max-depth", type=int, default=1,
                      help="show sizes down to this depth (default 1)")
    p_du.add_argument("-a", "--all", action="store_true",
                      help="also list individual files, not just directories")

    p_dl = sub.add_parser("download", parents=[common],
                          help="download a single file or a directory")
    p_dl.add_argument("remote", nargs="?", default=None,
                      help="remote file or directory path")
    p_dl.add_argument("dest", nargs="?", default=".",
                      help="local destination (default: current dir)")
    p_dl.add_argument("--resume-only", action="store_true",
                      help="do not fetch anything new: only finish half-downloaded "
                           "files (*.part) already present under DEST")
    p_dl.add_argument("--verify", action="store_true",
                      help="after each download, verify integrity against the "
                           "NAS-computed MD5 (slower; needs SYNO.FileStation.MD5)")
    p_dl.add_argument("--checksum", action="store_true", help=CHECKSUM_HELP)
    p_dl.add_argument("-j", "--jobs", type=int, default=1,
                      help="number of files to download in parallel (default 1)")

    p_mkdir = sub.add_parser("mkdir", parents=[common],
                             help="create a folder on the NAS (writes to the NAS)")
    p_mkdir.add_argument("remote", help="remote folder path to create")
    p_mkdir.add_argument("-p", "--parents", action="store_true",
                         help="create missing parents and do not fail if it exists")

    p_up = sub.add_parser("upload", parents=[common],
                          help="recursively upload local -> NAS (writes to the NAS)")
    p_up.add_argument("local", help="local file or directory to upload")
    p_up.add_argument("remote", nargs="?", default="/",
                      help="remote destination folder")
    p_up.add_argument("--delete", action="store_true",
                      help="one-way mirror: also remove files ON THE NAS that no "
                           "longer exist locally (DESTRUCTIVE; asks first)")
    p_up.add_argument("--yes", action="store_true",
                      help="skip the --delete confirmation prompt (for scripts)")
    p_up.add_argument("--verify", action="store_true",
                      help="after each upload, verify against the NAS-computed MD5")
    p_up.add_argument("--checksum", action="store_true", help=CHECKSUM_HELP)
    p_up.add_argument("--truncate-names", action="store_true",
                      help="if a filename is too long for the NAS (measured in "
                           "UTF-8 BYTES, so non-ASCII names hit it sooner), upload "
                           "it under a shortened name instead of skipping it")
    p_up.add_argument("--max-name-bytes", type=int, default=DEFAULT_MAX_NAME_BYTES,
                      help=f"max filename length in bytes before it is considered "
                           f"too long (default {DEFAULT_MAX_NAME_BYTES})")
    p_up.add_argument("-j", "--jobs", type=int, default=1,
                      help="number of files to upload in parallel (default 1)")

    p_sync = sub.add_parser("sync", parents=[common],
                            help="recursively sync a remote directory")
    p_sync.add_argument("remote", nargs="?", default=None,
                        help="remote directory path")
    p_sync.add_argument("dest", nargs="?", default=".",
                        help="local destination directory (default: current dir)")
    p_sync.add_argument("--delete", action="store_true",
                        help="one-way mirror: also remove local files that no "
                             "longer exist on the remote (destructive on local)")
    p_sync.add_argument("--resume-only", action="store_true",
                        help="do not walk the remote: only finish half-downloaded "
                             "files (*.part) already present under DEST")
    p_sync.add_argument("--verify", action="store_true",
                        help="after each download, verify integrity against the "
                             "NAS-computed MD5 (slower; needs SYNO.FileStation.MD5)")
    p_sync.add_argument("--checksum", action="store_true", help=CHECKSUM_HELP)
    p_sync.add_argument("-j", "--jobs", type=int, default=1,
                        help="number of files to download in parallel (default 1)")
    return ap


GLOBAL_DEFAULTS = {"host": None, "user": None, "insecure": False,
                   "verbose": False, "quiet": False, "retries": 5, "direct": False}


def main():
    args = build_parser().parse_args()
    # Global options use SUPPRESS so they parse in either position (before or
    # after the subcommand); fill in the defaults for any that weren't given.
    for name, default in GLOBAL_DEFAULTS.items():
        if not hasattr(args, name):
            setattr(args, name, default)
    setup_logging(-1 if args.quiet else (1 if args.verbose else 0))

    host = args.host or os.environ.get("SYNOLOGY_HOST") or os.environ.get("SYNOLOGY_URL")
    user = args.user or os.environ.get("SYNOLOGY_USER")
    password = os.environ.get("SYNOLOGY_PASSWORD")

    if not host:
        sys.exit("No host. Set $SYNOLOGY_HOST or pass --host.")

    # 'resolve' and 'qcinfo' are pre-login diagnostics: they need only the host/ID.
    # (Both go through _resolve_or_exit(), which already reports network/ID errors.)
    if args.command == "resolve":
        cmd_resolve(host)
        return
    if args.command == "qcinfo":
        cmd_qcinfo(host, as_json=args.json)
        return

    if not user:
        sys.exit("No user. Set $SYNOLOGY_USER or pass --user.")
    if not password:
        password = getpass.getpass(f"Password for {user}: ")

    verify = not args.insecure

    # Try to bypass the relay: swap the host for a direct endpoint if one works.
    if args.direct:
        log.info("Looking for a direct path (bypassing the QuickConnect relay) ...")
        try:
            found = qc_find_direct(host)
        except requests.RequestException as e:
            log.warning("Direct-path lookup failed (%s); falling back to the relay.", e)
            found = None
        if found:
            host, direct_verify, label = found
            verify = verify and direct_verify
            log.info("Direct path via '%s': %s", label, host)
        else:
            log.info("No direct path found; falling back to the relay.")

    if not verify:
        requests.packages.urllib3.disable_warnings()  # type: ignore

    syno = Synology(host, verify=verify)
    install_interrupt_handler()
    log.info("Connecting to %s ...", syno.base)

    handlers = {
        "shares": cmd_shares,
        "ping": cmd_ping,
        "info": cmd_info,
        "stat": cmd_stat,
        "md5": cmd_md5,
        "search": cmd_search,
        "ls": cmd_ls,
        "du": cmd_du,
        "mkdir": cmd_mkdir,
        "download": cmd_download,
        "sync": cmd_sync,
        "upload": cmd_upload,
    }
    logged_in = False
    try:
        syno.discover()
        syno.login(user, password)
        logged_in = True
        rc = handlers[args.command](syno, args)
        if interrupted():
            # Exit 130 (128+SIGINT) so scripts and CI can tell a Ctrl-C apart
            # from a clean, complete run.
            sys.exit(130)
        if rc:
            # Some items failed (error/corrupt/incomplete/toolong): exit
            # non-zero so cron and scripts can detect a failed transfer.
            sys.exit(int(rc))
    except KeyboardInterrupt:                # only if the default handler ran
        log.warning("Interrupted. Any .part files remain and will resume next run.")
        sys.exit(130)
    except SynoError as e:
        sys.exit(f"error: {err_text(e)}")
    except requests.exceptions.SSLError as e:
        sys.exit(f"error: TLS verification failed: {e}\n"
                 "       hint: for a self-signed NAS certificate, pass --insecure.")
    except requests.exceptions.Timeout as e:
        sys.exit(f"error: timed out talking to {syno.base}: {e}")
    except requests.RequestException as e:
        sys.exit(f"error: network error talking to {syno.base}: {e}")
    finally:
        if logged_in:
            syno.logout()


if __name__ == "__main__":
    main()
