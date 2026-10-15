# TODO

## Pending — needs a live NAS to verify before shipping

- [ ] **Login over POST instead of GET.** `Synology.login()` currently sends
      `account` + `passwd` as GET query params (via `_get_json`), so credentials
      can end up in relay/proxy/server access logs. The DSM API also accepts
      POST for `SYNO.API.Auth.login`; switching is trivial, but it MUST be
      tested against a real NAS (both relay and `--direct` paths, with and
      without 2FA) before changing it — no live server available right now.
      Note: since the review fixes, the password is at least redacted from the
      `-v` debug log (`Synology._redacted`).
- [ ] **Verify the recent fixes against a live DSM** (unit tests cover the
      logic, but not the wire):
      - `_stream_once` HTTP-error path (expire a session mid-download and check
        the `.part` is kept and the error is clear, not `incomplete`) — both
        the non-200 case and the status-200 `text/html` captive/landing page;
      - empty-directory sync (`sync` recreates empty remote dirs; `--delete`
        does not prune them);
      - non-zero exit code on failed/corrupt/incomplete items (run from cron),
        including failed NAS deletes in `upload --delete`;
      - `--delete` skip after Ctrl-C (interrupt a `sync --delete` mid-walk and
        confirm nothing local is removed; interrupt mid-delete-pass and confirm
        no new deletes are issued);
      - `upload --delete` with a too-long local name: the NAS copy (uploaded
        earlier under a truncated name) must SURVIVE.

## Ideas (from `docs/library-and-backends.md`)

- [ ] Tests first (done — `tests/`), then extract the shared level-1 infra
      (logging, progress, Ctrl-C, `run_many`, summary) into a library shared
      with `nextcloudfs`; decide level 2 (`Backend` interface) afterwards.
- [ ] QNAP backend as the first proof of the abstraction.
- [x] License: **GPLv3** (decided 2026-10-15, published on GitHub).
- [ ] Package/distribution name for the future shared library (still open).
