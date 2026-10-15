# synofs

A small, dependency-light command-line client for Synology NAS over the **DSM
Web API** (`SYNO.API.Auth` + `SYNO.FileStation.*`) — think `rsync`-style
transfers for places where SSH/rsync are not available, typically behind
**QuickConnect** (`https://YOURID.quickconnect.to`).

Its unique trick: it can **bypass the slow QuickConnect relay** by asking
Synology's coordinator for the NAS's own LAN/WAN/DDNS/SmartDNS addresses and
connecting to the first that answers (`--direct`).

Single file, pure Python 3, only needs `requests`.

## Features

- **Read commands** (never write to the NAS): `shares`, `ls`, `du`, `stat`,
  `md5`, `search`, plus diagnostics: `ping`, `info`, `resolve`, `qcinfo`.
- **Write commands** (explicit, never guessed): `download`/`sync` write only
  locally; `upload`/`mkdir` are the only commands that write to the NAS. Both
  transfer directions are additive by default (new/changed files only);
  `--delete` turns either into a one-way mirror.
- `--verify`: after each transfer, check the file against the **NAS-computed**
  MD5 (mismatches are reported as `corrupt`).
- `--checksum`: decide *what to transfer* by MD5 instead of size+mtime (slower:
  one on-demand `md5` task per file — the NAS has no precomputed checksums).
- **Resumable, consistent downloads**:
  - skips files already present with the same size and date,
  - resumes partial downloads with HTTP `Range` (safely re-downloads if the
    server ignores `Range`),
  - **auto-retries within the same run** on a dropped connection, continuing
    from the bytes already on disk (`--retries`, default 5, exponential backoff),
  - writes to `<file>.part` with a `.part.json` sidecar and only renames
    atomically when complete — a final file is never half-written, and only a
    sidecar-verified `.part` is ever resumed (orphans are wiped),
  - `--resume-only` finishes the half-downloaded files under a directory
    without walking the remote or fetching anything new.
  - Uploads are **not** resumable (the File Station API has no chunked upload):
    an interrupted file restarts from zero.
- `-j/--jobs N`: transfer N files in parallel, each with its own live progress
  bar (degrades to plain lines when not a terminal).
- **Empty directories are mirrored too**: `sync` recreates the remote
  directory tree locally, including empty folders, and `--delete` never prunes
  a folder that exists on the remote.
- **Safe interruption**: the first Ctrl-C finishes in-flight transfers and
  starts no new ones (everything stays resumable); the second aborts
  immediately. `--delete` mirroring is **skipped** if the walk was interrupted,
  since deleting against an incomplete file list could destroy good data.
- **End-of-run failure report**: every failed item (error / corrupt /
  incomplete / name-too-long) is listed again *after* the summary with its
  reason — in a multi-hour transfer you never have to scroll back to find it.
- **Script-friendly exit codes** (see below) and `--yes` for unattended
  `upload --delete`.
- **Long filenames**: names are limited by UTF-8 *bytes* (255 per segment), so
  CJK names hit the wall early. `upload` skips over-long names with a clear
  message (`toolong`); `--truncate-names` uploads them under a shortened,
  stable, collision-resistant name instead (`--max-name-bytes` tunes the
  threshold, default 240). A too-long file that is skipped still protects its
  NAS counterpart (raw or truncated name) from `upload --delete`.
- **Clear errors, no tracebacks**: network/TLS/API failures become a single
  `error: …` line with a hint; DSM error codes are translated to human text.

## Install

```bash
pip install -r requirements.txt   # just 'requests'
```

Requires Python 3.8+.

## Configure

```bash
cp .env.example .env
$EDITOR .env
source .env
```

| Variable            | Meaning                                   |
|---------------------|-------------------------------------------|
| `SYNOLOGY_HOST`     | NAS URL, e.g. `https://YOURID.quickconnect.to` |
| `SYNOLOGY_USER`     | username                                  |
| `SYNOLOGY_PASSWORD` | optional; prompted interactively if unset |

`--host` / `--user` override the env vars. The password is **never** taken
from the command line (see [Privacy](#privacy)).

## Global options

These apply to every subcommand and work **in either position** — before or
after the subcommand (`synofs.py --host X resolve` and `synofs.py resolve --host X`
are equivalent):

| Option            | Meaning                                                     |
|-------------------|-------------------------------------------------------------|
| `-v`, `--verbose` | DEBUG logging on stderr: show every API call, login/logout |
| `-q`, `--quiet`   | quiet: only warnings and errors on stderr                  |
| `--insecure`      | do not verify the TLS certificate (self-signed NAS cert)   |
| `--direct`        | bypass the QuickConnect relay (see below)                  |
| `--retries N`     | times to retry+resume a file after a drop (default 5)      |

By default synofs prints short `-> …` progress lines to stderr. `-v` adds a
`·  …` line for each API call (handy when a request fails); `-q` keeps only
`!  warnings` and `error:` lines.

## Usage

```bash
# what QuickConnect exposes about the NAS, WITHOUT logging in (privacy audit)
./synofs.py qcinfo --host YOURID.quickconnect.to
./synofs.py qcinfo --json

# check the connection + login and show what NAS you're talking to
./synofs.py ping

# dump everything the API exposes about the NAS (add --json for machine output)
./synofs.py info
./synofs.py info --json

# list the shares you can access
./synofs.py shares

# list a directory
./synofs.py ls /homes/alice

# detailed metadata for a path
./synofs.py stat /homes/alice/report.pdf

# NAS-computed MD5 of a file
./synofs.py md5 /homes/alice/report.pdf

# recursive filename search (all shares, or under a path)
./synofs.py search '*.pdf'
./synofs.py search '*.pdf' /homes/alice

# disk usage of a directory (immediate children + total)
./synofs.py du /homes/alice

# ...deeper, and including individual files
./synofs.py du /homes/alice -d 2 -a

# download a single file into the current dir
./synofs.py download /homes/alice/report.pdf .

# download a whole directory (creates ./dir2/…)
./synofs.py download /homes/alice/dir1/dir2 .

# one-way sync a directory into . (additive: only pulls new/changed files)
./synofs.py sync /homes/alice/dir1/dir2 .

# ...verifying each downloaded file against the NAS-computed MD5
./synofs.py sync /homes/alice/dir1/dir2 . --verify

# ...downloading 4 files in parallel
./synofs.py sync /homes/alice/dir1/dir2 . -j 4

# same, but with trailing slash -> sync the *contents* into .
./synofs.py sync /homes/alice/dir1/dir2/ .

# one-way MIRROR: also delete local files that no longer exist on the remote
./synofs.py sync /homes/alice/dir1/dir2 . --delete

# --- writing to the NAS -------------------------------------------------
# create a folder (-p creates parents)
./synofs.py mkdir -p /homes/alice/backup/2025

# upload a directory (creates /homes/alice/backup/photos/...)
./synofs.py upload ./photos /homes/alice/backup

# trailing slash -> upload the CONTENTS into the target
./synofs.py upload ./photos/ /homes/alice/backup

# 4 in parallel, verifying each against the NAS-computed MD5
./synofs.py upload ./photos /homes/alice/backup -j 4 --verify

# one-way MIRROR onto the NAS: also DELETES files there (asks first)
./synofs.py upload ./photos /homes/alice/backup --delete

# names too long for the NAS filesystem: shorten them instead of skipping
./synofs.py upload ./chinese-docs /homes/alice/backup --truncate-names

# resume a single interrupted file (finds ./3.E01.part and continues it)
./synofs.py download /homes/alice/3.E01 .

# finish ONLY the half-downloaded files under a directory, no remote walk
./synofs.py download --resume-only ./somedir
./synofs.py sync --resume-only .
```

`sync` path semantics follow rsync's trailing-slash rule:

| Command                                 | Result          |
|-----------------------------------------|-----------------|
| `sync /homes/alice/dir1/dir2  .`        | `./dir2/…`      |
| `sync /homes/alice/dir1/dir2/ .`        | `./…` (contents)|

`upload` applies the same rule to the **local** side:

| Command                                 | Result                    |
|-----------------------------------------|---------------------------|
| `upload ./photos  /homes/alice`         | `/homes/alice/photos/…`   |
| `upload ./photos/ /homes/alice`         | `/homes/alice/…`          |

### Interrupting a run

Ctrl-C is two-stage, so you are never forced to choose between waiting forever
and killing a transfer mid-flight:

| | What happens |
|---|---|
| **First Ctrl-C** | *Dies cleanly*: transfers already in flight finish, no new ones start, and the summary reports the rest as `cancelled`. |
| **Second Ctrl-C** | *Dies badly*: aborts immediately, right where it is. |

Either way the run exits with status **130**. Downloads resume from their
`.part` files on the next run; uploads restart the interrupted file (the File
Station API has no resumable upload). A `--delete` mirror pass is **skipped**
after an interruption: deleting against a file list that was cut short could
remove perfectly good data. If the interruption arrives while the delete pass
is already running, no *new* deletes are issued — re-run the same command to
finish it.

### Exit codes

| Code | Meaning |
|------|---------|
| `0`  | success (skipped/unchanged files are success) |
| `1`  | at least one item failed: `error`, `corrupt`, `incomplete` or `toolong` (see the end-of-run failure report), or a fatal connection/auth/usage error |
| `2`  | command-line usage error (argparse) |
| `130`| interrupted with Ctrl-C |

### `--insecure`

Only needed if the NAS presents a self-signed certificate (typically **not**
the case over QuickConnect, which uses a valid `*.quickconnect.to` certificate).

## Bypassing the QuickConnect relay (`--direct`)

QuickConnect normally tries LAN, then port-forwarded WAN, and only falls back to
Synology's **relay** (slow, shared) as a last resort. Using the plain
`xxx.quickconnect.to` URL forces the relay. synofs can ask Synology's
coordinator for the NAS's own addresses and connect directly:

```bash
# diagnose: list direct endpoints and which respond (no login needed)
./synofs.py resolve
# or without env vars set:
./synofs.py --host mynas.quickconnect.to resolve

# actually connect directly, bypassing the relay
./synofs.py --direct ls /homes/alice
./synofs.py --direct sync /homes/alice/dir1/dir2 . -j 4
```

`--direct` resolves the QuickConnect ID, probes each candidate in order
(SmartDNS with a valid cert → LAN IP → DDNS → WAN IP) and uses the first that
answers; if none do, it transparently falls back to the relay. Raw-IP endpoints
are used with TLS verification disabled (their certificate won't match the IP);
SmartDNS/DDNS endpoints keep a valid certificate.

A direct path only exists if the NAS is reachable that way — you're on its LAN,
or the owner set up port-forwarding / DDNS. A pure relay-only NAS has no direct
path, and `resolve` will tell you so.

## Privacy

What this tool exposes, and what it doesn't:

- **Credentials never go through argv.** Host/user come from env vars or flags;
  the password comes from `SYNOLOGY_PASSWORD` or an interactive prompt, so it
  stays out of your shell history and `ps`. With `-v`, the password and OTP
  are **redacted** (`***`) in the debug log.
- **QuickConnect leaks more than you think — to Synology, not to us.** Anyone
  who knows a QuickConnect ID can query Synology's public coordinator and get
  back the NAS's public WAN IP, its LAN IPs/interfaces, DDNS names, SmartDNS
  hostnames and relay info, **without any authentication**. Run
  `./synofs.py qcinfo` against your own ID to see exactly what is exposed —
  that command is deliberately pre-login so it doubles as an audit. If that
  exposure bothers you, use a VPN (WireGuard/Tailscale) instead of
  QuickConnect, or at least a non-guessable ID.
- **`--direct` with raw IPs disables TLS verification** for those candidates
  (the NAS certificate can't match a bare IP), so that specific path is
  vulnerable to a local MITM. SmartDNS/DDNS candidates keep full verification.
  Relay mode (`xxx.quickconnect.to`) uses Synology's valid certificate.
- **Known limitation**: the DSM login call sends credentials as URL query
  parameters over TLS (the form Synology's own API guide documents). They are
  encrypted in transit, but could appear in intermediate access logs
  (relay/proxy). Switching to POST is tracked in `TODO.md`.
- synofs itself phones home nowhere: the only endpoints it talks to are your
  NAS and (for `resolve`/`qcinfo`/`--direct`) Synology's QuickConnect
  coordinator.

## Notes / limitations

- If your QuickConnect ID redirects to a different host before reaching DSM, the
  Web API may not answer on the `quickconnect.to` URL itself. synofs detects this
  (the endpoint returns HTML/an error page instead of JSON) and reports
  `expected JSON but got HTTP … (text/html)` with a hint, instead of crashing.
  In that case use the host you land on after login as `SYNOLOGY_HOST`, or try
  `--direct` / the `resolve` command to find a working endpoint. Run with `-v`
  to see the exact request that failed.
- 2FA (OTP) is supported: if the account requires it, you'll be prompted for the
  code at login (interactive sessions only — a non-interactive stdin fails with
  a clear error instead of a traceback).
- Resume depends on the server honoring HTTP `Range`. If it doesn't, synofs
  detects the `200` response and safely re-downloads the whole file. A
  mid-download HTTP error (e.g. an expired session returning an HTML page)
  never contaminates the `.part`: it is kept intact and resumes on a later run.
- **Symlinks are skipped on upload** (files *and* directories; like `rsync`
  without `-l/-L`): the File Station API uploads regular files, and following
  directory links risks loops and surprises. Skipped links are reported in the
  log (one aggregate line listing up to 5 paths).
- **Naming caveat**: `.part` / `.part.json` suffixes are reserved for transfer
  temporaries. `upload` skips local files with those suffixes, `sync --delete`
  never prunes them, and `--resume-only` reads every `*.part.json` it finds as
  a resume sidecar (foreign ones are detected and skipped). Avoid shipping
  real files with those names inside a sync tree.
- **Long filenames**: the NAS filesystem limits a name to 255 **bytes** per path
  segment — measured in UTF-8 bytes, not characters, so non-ASCII names (Chinese,
  3 bytes/char) hit it much sooner than they look. By default `upload` **skips**
  an over-long file with a clear message and counts it as `toolong`; pass
  `--truncate-names` to upload it under a shortened name instead (extension kept,
  cut on a character boundary, a short hash keeps it unique and stable across
  re-runs). `--max-name-bytes` tunes the threshold (default 240).
- `--checksum` costs one on-demand `md5` task on the NAS per file — much slower
  than the default size+mtime check on big trees. If the NAS can't produce a
  checksum, synofs warns and falls back to a size-only comparison for that file.

## Development

```bash
pip install -r requirements-dev.txt   # requests + pytest
python -m pytest tests/ -q            # no network / no NAS needed
```

The test suite covers the pure helpers (path normalization, name fitting,
QuickConnect ID/candidate parsing, summaries), the download resume/restart
decision matrix, the mirror-delete rules, interrupt handling, and the parallel
runner — all driven with fakes. Wire-level behaviour against a real DSM is
tracked in `TODO.md`.

The code is a single file by design. Functions that implement protocol details
cite their source (`[FS-API]`, `[QC]`) in comments — see below.

## Credits / References

This tool doesn't invent any protocol — it wraps two bodies of knowledge that
belong to others. If the API or the QuickConnect protocol ever changes, these
are the sources to go back to (the code tags each relevant function with `[FS-API]`
or `[QC]` so you can find the origin quickly):

- **`[FS-API]` — Synology File Station Official API** (Synology's own developer
  documentation). Source for the `SYNO.API.*` / `SYNO.FileStation.*` endpoints,
  the `/webapi/query.cgi` + `SYNO.API.Info` discovery handshake, and every error
  code:
  <https://global.download.synology.com/download/Document/Software/DeveloperGuide/Package/FileStation/All/enu/Synology_File_Station_API_Guide.pdf>

- **`[QC]` — QuickConnect protocol** (NOT officially documented; reverse-engineered
  by the community). Source for the `global.quickconnect.to/Serv.php`
  `get_server_info` request, the `server_info` field layout, and the
  LAN → WAN → relay connection ordering used by `resolve` / `--direct`:
  - [nullvoidptr/qcon](https://github.com/nullvoidptr/qcon/blob/master/protocol.md)
    — Go library + `protocol.md` (MIT)
  - [Thomas Theunen, *Diving into Synology QuickConnect*](http://www.thomastheunen.eu/2017/07/diving-into-synology-quickconnect-and.html)
    (2017)
  - [mamascode, *Synology QuickConnect Protocol*](https://mamascode.wordpress.com/2015/10/31/synology-quickconnect-protocol/)
    (2015)

All product names and the QuickConnect service are trademarks of Synology Inc.;
this project is not affiliated with or endorsed by Synology.

## License

**GPLv3** — see [LICENSE](LICENSE). No code was copied from any source: the
tool was implemented from the protocol documentation cited above (protocol
facts aren't copyrightable, and the QuickConnect reverse-engineering references
we leaned on are MIT/permissive).
