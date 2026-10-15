# Shared library & multi-backend — research notes and pending decisions

**Status:** ideas / not started. Captured 2026-07-21. Nothing here is committed to;
this is the input for a future decision. Two questions are still blocked:
license/ownership (waiting on an ICIJ standup) and the package name.

This concerns two tools that already work and are tested against live servers:

- `synofs` (`~/workspace/synofs`) — Synology File Station over the DSM Web API,
  QuickConnect-friendly. Read + `upload`/`mkdir`. Its unique trick: **bypassing
  the QuickConnect relay** by resolving the NAS's own LAN/WAN/DDNS address.
- `nextcloudfs` (`~/workspace/nextcloudfs`) — Nextcloud over WebDAV, app-password
  auth, resumable chunked upload.

---

## 1. Why a library — the duplication is real

Measured with an AST diff of both files (functions/classes, similarity ratio):

- **~515 lines duplicated (~29% of the smaller tool).**
- **19 units are byte-identical**, e.g. `setup_logging`, the whole two-stage
  Ctrl-C handler, `Progress`/`SlotReporter`/`fmt_bar`/`_ProgressReader`,
  `run_many`, `print_summary`/`new_stats`/`STATUS_*`, `human`, `fmt_time`,
  `file_md5`, `print_json`, `_rm`, `_finalize`, the mirror-delete logic.

Two levels of extraction:

- **Level 1 — infrastructure (~500 lines), near-zero risk.** Logging, progress
  UI, Ctrl-C handling, the parallel `run_many` runner, stats/summary, small
  helpers. Pure code-move.
- **Level 2 — real design (~400-600 more).** The resumable-download engine
  (`.part` + sidecar + HTTP Range) and the generic commands (`ls`/`du`/`stat`/
  `download`/`sync`/`upload`/`mkdir`) are algorithmically identical between the
  two; only *how the HTTP request is issued* and *how errors are classified*
  differ. A `Backend` interface (`stat`/`listdir`/`walk`/`open_range`/`upload`/
  `mkdir`/`delete`) lets the commands be written once. Payoff: a new backend
  becomes small.

**Recommended order: tests first → level 1 → decide level 2.** Refactoring two
working, live-tested tools with zero automated tests is how things break
silently — and the tests to write (path semantics, WebDAV XML parsing, chunked
upload, interrupt logic, `run_many` accounting) are exactly the ones that would
validate the refactor. Tests do not depend on the license/name decision.

---

## 2. The elephant: rclone already exists

`rclone` is one binary, 70+ backends, resumable transfers, checksums, `sync
--delete`, parallelism. It covers WebDAV (with Nextcloud chunked upload), S3,
Google Drive, and reaches most NAS via SMB/SFTP. If the goal were "a generic
multi-backend transfer tool," **it already exists and is mature.**

What is genuinely ours and not in rclone:

- **The QuickConnect relay bypass** (querying Synology's coordinator for the
  NAS's direct addresses, following regional coordinators, the SmartDNS parsing).
  No generic client does this.
- Well-done **path-based proprietary HTTP APIs** (Synology, QNAP) that rclone
  only reaches awkwardly or via SMB/SFTP.

So the value question is not "how many backends can we support" but **"what do we
do that rclone doesn't."**

---

## 3. Everything falls into three families

The decisive finding from the research: **there is no single identity model.**

| Family | Identity | Backend approach | Systems |
|---|---|---|---|
| **① Path-based proprietary HTTP** | path = identity | one custom backend per system | **Synology, QNAP, Nextcloud / ownCloud 10 / oCIS, Seafile** |
| **② Generic WebDAV** | path = identity | **one backend, many servers** | Asustor, TerraMaster, UGREEN, Apache mod_dav, SabreDAV, Baïkal, `rclone serve webdav`, Caddy |
| **③ Object / ID store** | opaque key or ID, **not path** | different model entirely | S3 / MinIO / Ceph RGW / Garage / B2, Google Drive, MEGA, WD My Cloud Home, Pydio Cells |

Family ③ **structurally breaks** a path abstraction:

- **Google Drive & MEGA** allow *duplicate names in the same folder* — a path
  does not uniquely identify a file (addressed by fileId / node handle).
- **S3-family** is a flat keyspace: `a/b` and `a/b/c` can coexist (a key is both
  file and prefix), no real directories, no settable mtime, ETag ≠ whole-file MD5
  on multipart / SSE.
- These fit an *object/key* abstraction, not a path one — and rclone already
  covers them.

---

## 4. Capability matrix (what matters for the abstraction)

Legend: ✅ yes · ⚠️ with caveat · ❌ no · ? unverified. "Headless" = works from
cron without an interactive browser.

### Family ① — path-based proprietary HTTP

| System | Headless auth | Range dl | Resumable upload | Server checksum | Set mtime | API status |
|---|---|---|---|---|---|---|
| **Synology** (have it) | session (user+pass) | ✅ Range | ❌ single POST | ✅ MD5 (async) | ✅ ms | reverse-eng. + FS-API PDF |
| **QNAP** ⭐ | `sid` session | ✅ via `offset` | ✅ 3-step chunked, resumable | ✅ MD5 | ✅ `settime` | **officially documented** |
| **Nextcloud** (have it) | app-password (Basic) | ⚠️ Range (breaks under SSE/obj) | ✅ chunk v2 (MKCOL/PUT/MOVE) | ⚠️ `OC-Checksum` *stored unvalidated* on plain PUT | ✅ `X-OC-Mtime` | official |
| **ownCloud 10** | app-password (Basic) | ⚠️ Range | ✅ ChunkingNG v2 | ✅ `OC-Checksum` *validated* on PUT/MOVE | ✅ `X-OC-Mtime` | official |
| **oCIS** | app-token (Basic) / OIDC | ✅ Range | ✅ **TUS 1.0** (only standard one) | ✅ SHA1/MD5 | ✅ | official; needs space/drive-ID resolution first |
| **Seafile** | token / repo-token | ✅ 206 | ✅ custom (offset anchor) | ✅ content-addressed id (best fingerprint) | ✅ `last_modify` form field | official; per-**library UUID** root; auto-renames on collision |

QNAP is the standout new backend: the only vendor with an officially documented
HTTP file API providing *every* primitive natively — offset (resumable)
download, real 3-step resumable chunked upload with server-side commit tracking,
server MD5, mtime-set, real paths. Effectively synofs with fewer limits.

### Family ② — generic WebDAV (one backend serves all)

- Real hierarchical paths, unique names (RFC 4918: at most one mapping per
  segment). Range GET is optional but universally present → 206.
- **Resumable upload is non-portable:** Apache mod_dav accepts partial PUT via
  `Content-Range: bytes START-END/*`; SabreDAV rejects that (501) and offers its
  own `PATCH` partial-update; `rclone serve webdav`/Caddy have neither. So plain
  WebDAV = whole-file PUT unless you special-case the server.
- **No checksums and no settable mtime** in stock WebDAV — only the ownCloud
  extension headers (`X-OC-Mtime`, `OC-Checksum`) rescue owncloud/nextcloud-
  vendor servers. `rclone serve webdav --etag-hash` can expose a real hash.
- NAS with **no HTTP file API at all** (use their WebDAV/SFTP/rsync instead):
  **Asustor, TerraMaster, UGREEN, Buffalo**. OpenMediaVault and Zyxel are
  non-starters for HTTP (OMV has no generic file API; Zyxel is EOL).
- **TrueNAS**: has an API but crippled for this — no Range-resume, no chunked
  upload, no server checksum, no mtime-set, admin-only, and it moves to a
  WebSocket JSON-RPC API in v26. Marginal.

### Family ③ — object / ID stores (do NOT fit path abstraction)

- **S3 / MinIO / Ceph RGW / Garage / Backblaze B2**: flat keyspace, no real
  dirs, no mtime (only `x-amz-meta-*` / `X-Bz-Info-*` conventions), ETag
  unreliable as a whole-file hash (multipart = composite `-N`; SSE-C/KMS ⇒ not
  MD5). Use explicit `x-amz-checksum-*` or B2 SHA1. Versioned stores allow
  multiple objects per name. Headless auth is *excellent* (static keys) — but the
  model is object/key, not path.
- **Google Drive**: fileId identity, duplicate names legal, shortcuts, Shared
  Drives, Google-native docs (export-only, no checksum). OAuth browser once (OOB
  paste flow dead since 2023-01-31) or service-account-with-Shared-Drive (7-day
  token trap in "Testing" status). The archetypal non-path backend.
- **MEGA**: opaque node handles, duplicate names legal, client-side E2E crypto,
  no OAuth/API-token (only a saved session string), no conventional server
  checksum, effectively no mtime. Structurally unlike everything else.
- **WD My Cloud Home**: Drive-shaped (id/parentID), OAuth2, has range GET +
  resumable PUT, but flat object store, checksum/mtime unverified.
- **Pydio Cells**: logically path-ish but UUID-keyed over a flat S3 datasource,
  bytes via S3 gateway (composite ETag), case-insensitive, mtime unverified.

### One universal caveat

**None** of the four self-hosted suites (Seafile, ownCloud 10, oCIS, Nextcloud)
has a real **delta/changes API**. A sync client must recursively ETag-poll with
`Depth: 1` (Depth:infinity is disabled or unreliable everywhere).

---

## 5. Recommendation

- **Yes to the library.** Scope = **families ① + ② only** (the path-based
  world). Design a `Backend` interface (`stat`/`listdir`/`walk`/`open_range`/
  `upload`/`mkdir`/`delete`) for the path model; do **not** try to make it also
  fit ③.
- **Starter backends:** Synology + Nextcloud/generic-WebDAV (already built) →
  **QNAP** as the first new backend that proves the abstraction earns its keep.
- **Explicitly out of scope:** S3/object, Google Drive, MEGA. Different identity
  model, and rclone already covers them. Stating this is focus, not defeat.
- **Keep and highlight the QuickConnect relay bypass** — it is the real
  differentiator; nothing generic does it.
- **Tests first**, then level-1 extraction, then reassess level 2 with the
  library actually in motion.

---

## 6. Open decisions (blocked / to revisit)

1. **Ownership / license** — waiting on the ICIJ standup. If the tools were built
   in work time / for a work need, copyright may be ICIJ's, not personal. No
   `LICENSE` file until this is clear (a wrong name is worse than none). Nothing
   we depend on imposes any obligation: we copied no code, only implemented from
   protocol specs (facts aren't copyrightable); deps are all permissive
   (requests Apache-2.0, urllib3/charset-normalizer MIT, idna BSD-3, certifi
   MPL-2.0 but unmodified); the QuickConnect reverse-engineering we leaned on
   (qcon) is MIT. Leaning MIT; Apache-2.0 if external PRs are expected.
2. **Package/distribution name** — pick a neutral, renameable name until #1 is
   resolved.
3. **Library scope level** — 1 (infra only) vs 2 (Backend interface + generic
   commands). Lean toward 2 *after* tests exist.
4. **Repo structure** — monorepo with one package + two entry points
   (`console_scripts`), vs three packages, vs separate repos. Leaning monorepo /
   one package / two entry points.
5. **The real target set for ICIJ** — worth confirming which systems ICIJ
   actually moves files with. Suspicion: Nextcloud, S3/MinIO, Google Drive,
   SharePoint (and maybe Aleph/Datashare) matter more than consumer NAS. If so,
   the S3/Drive "family ③" question reopens and might argue for *using rclone*
   for those rather than reimplementing.

---

## 7. Sources

- Synology File Station API Guide (official PDF).
- QNAP QTS File Station HTTP API v5 (official PDF):
  `https://download.qnap.com/dev/QNAP_QTS_File_Station_API_v5.pdf`.
- Nextcloud / ownCloud / oCIS developer manuals (WebDAV, chunking, OCS, TUS).
- Seafile web API (`https://cloud.seafile.com/published/web-api/home.md`).
- RFC 4918 (WebDAV), RFC 7233 (Range).
- QuickConnect reverse-engineering: nullvoidptr/qcon (MIT) + protocol.md;
  Thomas Theunen (2017); mamascode (2015).
- Cloud providers: Dropbox / Google Drive v3 / MS Graph / Box / pCloud / MEGA /
  Koofr / Jottacloud / S3 developer docs (see per-provider notes in git history
  of this file if expanded later).
- Many NAS/self-hosted specifics flagged **unverified** in the research — re-check
  against primary docs before relying on them when a backend is actually built.
