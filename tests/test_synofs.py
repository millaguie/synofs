"""Tests for synofs.py.

No network, no real NAS: the pure helpers are tested directly, and the
transfer/resume/mirror logic is driven with fakes (a Synology subclass whose
_stream_once is stubbed, monkeypatched module functions, tmp_path trees).

Run with:  pytest            (from the repo root)
"""
import argparse
import json
import logging
import os

import pytest

import synofs
from synofs import (Synology, SynoError, _finish_partials, _fmt_time,
                    _mirror_delete, as_list, clean_remote, elide_middle,
                    fit_name, fit_rel_path, fmt_entry, human, name_bytes,
                    new_stats, print_summary, qc_extract_id, rel_has_long_segment,
                    run_many)


@pytest.fixture(autouse=True)
def clear_interrupt():
    """Every test starts and ends with the Ctrl-C flag unset."""
    synofs._interrupt.clear()
    yield
    synofs._interrupt.clear()


# ---- pure helpers ----------------------------------------------------------

def test_clean_remote():
    assert clean_remote("/a/b") == "/a/b"
    assert clean_remote("a/b/") == "/a/b"
    assert clean_remote("//a//b//") == "/a/b"
    assert clean_remote("") == "/"
    assert clean_remote(None) == "/"
    assert clean_remote("/") == "/"


def test_human():
    assert human(0) == "0B"
    assert human(1023) == "1023B"
    assert human(1024) == "1.0K"
    assert human(1536) == "1.5K"
    assert human(5 * 1024 ** 3) == "5.0G"
    assert human(3 * 1024 ** 4) == "3.0T"


def test_elide_middle():
    assert elide_middle("abcdef", 10) == "abcdef"
    assert elide_middle("abcdef", 6) == "abcdef"
    e = elide_middle("a" * 50, 11)
    assert len(e) == 11 and "…" in e
    assert elide_middle("abc", 0) == ""
    assert elide_middle("abc", 1) == "…"


def test_as_list():
    assert as_list(None) == []
    assert as_list("") == []
    assert as_list("x") == ["x"]
    assert as_list(["x", "y"]) == ["x", "y"]


def test_fmt_time():
    assert _fmt_time(0) == "-"
    assert _fmt_time(None) == "-"
    assert _fmt_time("nope") == "-"
    assert _fmt_time(1700000000).startswith("2023-")


def test_fmt_entry():
    f = {"path": "/a/b.txt", "name": "b.txt", "isdir": False,
         "size": 2048, "mtime": 0}
    line = fmt_entry(f)
    assert line.startswith("-") and "2.0K" in line and line.endswith("/a/b.txt")
    d = {"path": "/a", "name": "a", "isdir": True, "size": 0, "mtime": 0}
    assert fmt_entry(d).startswith("d")


# ---- QuickConnect ID extraction (dotted IDs are kept whole) ----------------

def test_qc_extract_id():
    assert qc_extract_id("https://mynas.quickconnect.to") == "mynas"
    assert qc_extract_id("http://mynas.quickconnect.to:5000/x") == "mynas"
    assert qc_extract_id("mynas.quickconnect.to") == "mynas"
    assert qc_extract_id("mynas") == "mynas"
    assert qc_extract_id("https://my.nas.quickconnect.to") == "my.nas"
    assert qc_extract_id("https://192.168.1.5:5001") == "192.168.1.5"


def test_qc_candidates_order_tls_and_dedup():
    info = {
        "server": {
            "interface": [{"name": "eth0", "ip": "192.168.1.10"},
                          {"name": "eth1", "ip": "169.254.1.1"}],
            "ddns": "my.synology.me",
            "external": {"ip": "1.2.3.4"},
        },
        "service": {"port": 5001, "ext_port": 12001},
        "smartdns": {"lan": ["a.direct.quickconnect.to"],
                     "external": "b.direct.quickconnect.to",   # string, not list
                     "host": "c.direct.quickconnect.to"},
    }
    cands = synofs.qc_candidates(info)
    urls = [u for _, u, _ in cands]
    verify = {u: v for _, u, v in cands}
    # SmartDNS first, with TLS verification
    assert urls[0] == "https://a.direct.quickconnect.to:5001"
    assert verify[urls[0]] is True
    assert "https://b.direct.quickconnect.to:12001" in urls
    assert "https://c.direct.quickconnect.to:5001" in urls
    # LAN IP: no verify; link-local is excluded
    assert "https://192.168.1.10:5001" in urls
    assert verify["https://192.168.1.10:5001"] is False
    assert all("169.254" not in u for u in urls)
    # DDNS (verified) and WAN IP (not verified)
    assert "https://my.synology.me:12001" in urls
    assert verify["https://my.synology.me:12001"] is True
    assert "https://1.2.3.4:12001" in urls
    assert verify["https://1.2.3.4:12001"] is False
    assert len(urls) == len(set(urls))          # de-duplicated


# ---- long-name fitting ------------------------------------------------------

def test_fit_name_short_unchanged():
    assert fit_name("photo.jpg", 240) == "photo.jpg"


def test_fit_name_ascii_fits_stable_and_unique():
    a = fit_name("x" * 300 + ".txt", 100)
    b = fit_name("x" * 300 + ".txt", 100)
    other = fit_name("y" * 300 + ".txt", 100)
    assert a == b                               # deterministic across runs
    assert name_bytes(a) <= 100
    assert a.endswith(".txt")                   # extension preserved
    assert "~" in a                             # hash tag present
    assert a != other                           # different names don't collide


def test_fit_name_multibyte_cut_on_char_boundary():
    out = fit_name("文件名" * 100 + ".pdf", 60)  # 9 bytes per CJK char
    assert name_bytes(out) <= 60
    assert out.endswith(".pdf")
    assert out.encode("utf-8").decode("utf-8")  # valid UTF-8, no mojibake


def test_fit_name_tiny_budget_never_exceeds():
    name = "x" * 300 + ".extremelylongextension"
    for budget in (1, 4, 7, 10, 20, 47, 48):
        out = fit_name(name, budget)
        assert name_bytes(out) <= budget


def test_fit_rel_path_and_detection():
    rel = "dir/" + "y" * 300 + ".md"
    assert rel_has_long_segment(rel, 240)
    fitted = fit_rel_path(rel, 100)
    assert not rel_has_long_segment(fitted, 100)
    assert all(name_bytes(s) <= 100 for s in fitted.split("/"))


# ---- secret redaction in debug logs -----------------------------------------

def test_redacted_masks_secrets():
    params = {"account": "alice", "passwd": "s3cret",
              "otp_code": "123456", "session": "FileStation"}
    red = Synology._redacted(params)
    assert red["passwd"] == "***"
    assert red["otp_code"] == "***"
    assert red["account"] == "alice"
    assert Synology._redacted(None) is None
    assert Synology._redacted({"a": 1}) == {"a": 1}


class _FakeResp:
    headers = {"Content-Type": "application/json"}
    status_code = 200
    url = "https://nas.example/webapi/entry.cgi"
    text = ""

    def json(self):
        return {"success": True, "data": {}}

    def raise_for_status(self):
        pass


def test_call_never_logs_the_password(caplog):
    syno = Synology("https://nas.example")
    syno.apis = {"SYNO.API.Auth": {"path": "auth.cgi", "maxVersion": 6}}
    syno.session = type("S", (), {"get": lambda self, *a, **k: _FakeResp()})()
    with caplog.at_level(logging.DEBUG, logger="synofs"):
        syno.call("SYNO.API.Auth", "login",
                  {"account": "alice", "passwd": "hunter2"})
    assert "hunter2" not in caplog.text
    assert "***" in caplog.text


# ---- summary / exit codes ----------------------------------------------------

def test_print_summary_exit_code(capsys):
    assert print_summary(new_stats(), 0) == 0
    s = new_stats()
    s["downloaded"], s["skip"], s["deleted"] = 2, 3, 1
    assert print_summary(s, 10) == 0            # a clean run (skips are fine)
    for bad in ("error", "corrupt", "incomplete", "toolong"):
        s = new_stats()
        s[bad] = 1
        assert print_summary(s, 0) == 1, bad
    capsys.readouterr()


# ---- download: skip / resume / restart decisions ------------------------------

class FakeSyno(Synology):
    """A Synology whose network streaming is replaced by local writes."""

    def __init__(self):
        super().__init__("https://nas.example")
        self.stream_calls = []                  # resume_from of each pass
        self.fail_times = 0                     # simulate N dropped connections

    def _stream_once(self, remote_path, part, meta, size, resume_from,
                     on_progress):
        self.stream_calls.append(resume_from)
        if self.fail_times > 0:
            self.fail_times -= 1
            with open(part, "ab") as fh:      # some bytes land before the drop
                fh.write(b"x" * min(10, size - resume_from))
            raise synofs.IncompleteDownload("simulated drop")
        mode = "ab" if resume_from else "wb"
        with open(part, mode) as fh:
            fh.write(b"x" * (size - (resume_from if resume_from else 0)))


def _write_sidecar(local_path, remote="/r/f.bin", size=100, mtime=1234):
    with open(local_path + ".part.json", "w") as fh:
        json.dump({"remote": remote, "size": size, "mtime": mtime}, fh)


def test_download_skips_unchanged(tmp_path):
    syno = FakeSyno()
    p = tmp_path / "f.bin"
    p.write_bytes(b"x" * 100)
    os.utime(p, (1234, 1234))
    assert syno.download_file("/r/f.bin", 100, 1234, str(p)) == "skip"
    assert not syno.stream_calls


def test_download_resumes_verified_part(tmp_path):
    syno = FakeSyno()
    p = tmp_path / "f.bin"
    (tmp_path / "f.bin.part").write_bytes(b"x" * 40)
    _write_sidecar(str(p))
    assert syno.download_file("/r/f.bin", 100, 1234, str(p)) == "resumed"
    assert syno.stream_calls == [40]            # Range resumes at the byte mark
    assert p.read_bytes() == b"x" * 100
    assert int(os.path.getmtime(p)) == 1234     # remote mtime restored
    assert not (tmp_path / "f.bin.part").exists()
    assert not (tmp_path / "f.bin.part.json").exists()


def test_download_complete_part_just_renames(tmp_path):
    syno = FakeSyno()
    p = tmp_path / "f.bin"
    (tmp_path / "f.bin.part").write_bytes(b"x" * 100)
    _write_sidecar(str(p))
    assert syno.download_file("/r/f.bin", 100, 1234, str(p)) == "resumed"
    assert not syno.stream_calls                # no network at all
    assert p.read_bytes() == b"x" * 100


def test_download_orphan_part_wiped(tmp_path):
    syno = FakeSyno()
    p = tmp_path / "f.bin"
    (tmp_path / "f.bin.part").write_bytes(b"garbage")   # no sidecar: untrusted
    assert syno.download_file("/r/f.bin", 100, 1234, str(p)) == "downloaded"
    assert syno.stream_calls == [0]
    assert p.read_bytes() == b"x" * 100


def test_download_changed_remote_restarts(tmp_path):
    syno = FakeSyno()
    p = tmp_path / "f.bin"
    (tmp_path / "f.bin.part").write_bytes(b"x" * 40)
    _write_sidecar(str(p), size=999, mtime=1)   # signature of an OLD version
    assert syno.download_file("/r/f.bin", 100, 1234, str(p)) == "downloaded"
    assert syno.stream_calls == [0]


def test_download_oversized_part_wiped(tmp_path):
    syno = FakeSyno()
    p = tmp_path / "f.bin"
    (tmp_path / "f.bin.part").write_bytes(b"x" * 150)
    _write_sidecar(str(p))
    assert syno.download_file("/r/f.bin", 100, 1234, str(p)) == "downloaded"
    assert syno.stream_calls == [0]


def test_download_retries_then_incomplete_keeps_part(tmp_path, monkeypatch):
    monkeypatch.setattr(synofs.time, "sleep", lambda s: None)
    syno = FakeSyno()
    syno.fail_times = 99
    p = tmp_path / "f.bin"
    st = syno.download_file("/r/f.bin", 100, 1234, str(p), retries=2)
    assert st == "incomplete"
    assert len(syno.stream_calls) == 3          # initial + 2 retries
    assert (tmp_path / "f.bin.part").exists()   # kept for a later run
    assert (tmp_path / "f.bin.part.json").exists()


# ---- _stream_once: HTTP error pages must never land in the .part ------------

class FakeStreamResp:
    def __init__(self, status=200, ctype="application/octet-stream",
                 body=b"", json_body=None):
        self.status_code = status
        self.headers = {"Content-Type": ctype}
        self.url = "https://nas.example/webapi/entry.cgi"
        self.text = body.decode("utf-8", "replace")
        self._body, self._json = body, json_body
        self.closed = False

    def iter_content(self, chunk_size=1):
        yield self._body

    def json(self):
        if self._json is None:
            raise ValueError("not json")
        return self._json

    def close(self):
        self.closed = True


def _dl_syno():
    syno = Synology("https://nas.example")
    syno.apis = {"SYNO.FileStation.Download": {"path": "entry.cgi",
                                               "maxVersion": 2}}
    return syno


def test_stream_once_http_error_keeps_part(tmp_path, monkeypatch):
    syno = _dl_syno()
    resp = FakeStreamResp(401, "text/html", b"<html>session expired</html>")
    monkeypatch.setattr(syno, "call_stream", lambda *a, **k: resp)
    part = tmp_path / "f.bin.part"
    part.write_bytes(b"x" * 10)
    meta = tmp_path / "f.bin.part.json"
    meta.write_text("{}")
    with pytest.raises(SynoError) as ei:
        syno._stream_once("/r/f.bin", str(part), str(meta), 100, 10, None)
    assert "401" in str(ei.value.args[0])
    assert part.read_bytes() == b"x" * 10       # HTML never entered the .part
    assert meta.exists()
    assert resp.closed


def test_stream_once_json_error_deletes_part(tmp_path, monkeypatch):
    syno = _dl_syno()
    resp = FakeStreamResp(200, "application/json",
                          json_body={"success": False, "error": {"code": 408}})
    monkeypatch.setattr(syno, "call_stream", lambda *a, **k: resp)
    part = tmp_path / "f.bin.part"
    part.write_bytes(b"x")
    meta = tmp_path / "f.bin.part.json"
    meta.write_text("{}")
    with pytest.raises(SynoError) as ei:
        syno._stream_once("/r/f.bin", str(part), str(meta), 100, 0, None)
    assert "408" in str(ei.value.args[0])
    assert not part.exists() and not meta.exists()


def test_stream_once_resume_206_appends(tmp_path, monkeypatch):
    syno = _dl_syno()
    captured = {}
    resp = FakeStreamResp(206, body=b"world")

    def fake_call_stream(*a, **k):
        captured.update(k)
        return resp

    monkeypatch.setattr(syno, "call_stream", fake_call_stream)
    part = tmp_path / "f.bin.part"
    part.write_bytes(b"hello")
    meta = tmp_path / "f.bin.part.json"
    meta.write_text("{}")
    syno._stream_once("/r/f.bin", str(part), str(meta), 10, 5, None)
    assert captured["headers"] == {"Range": "bytes=5-"}
    assert part.read_bytes() == b"helloworld"


def test_stream_once_range_ignored_redownloads(tmp_path, monkeypatch):
    syno = _dl_syno()
    resp = FakeStreamResp(200, body=b"helloworld")   # 200: Range not honored
    monkeypatch.setattr(syno, "call_stream", lambda *a, **k: resp)
    part = tmp_path / "f.bin.part"
    part.write_bytes(b"junk-")
    meta = tmp_path / "f.bin.part.json"
    meta.write_text("{}")
    syno._stream_once("/r/f.bin", str(part), str(meta), 10, 5, None)
    assert part.read_bytes() == b"helloworld"       # started from scratch


# ---- --resume-only ------------------------------------------------------------

def test_finish_partials_resumes_sidecars(tmp_path):
    d = tmp_path / "scan"
    d.mkdir()
    (d / "a.bin.part").write_bytes(b"x" * 40)
    (d / "a.bin.part.json").write_text(json.dumps(
        {"remote": "/r/a.bin", "size": 100, "mtime": 1234}))
    rc = _finish_partials(FakeSyno(), str(d), retries=1)
    assert rc == 0
    assert (d / "a.bin").read_bytes() == b"x" * 100
    assert not (d / "a.bin.part").exists()


def test_finish_partials_bad_sidecars_skipped(tmp_path, capsys):
    d = tmp_path / "scan"
    d.mkdir()
    (d / "a.part.json").write_text(json.dumps({"remote": "/r/a"}))  # no size
    (d / "b.part.json").write_text("not json{{")
    rc = _finish_partials(None, str(d), retries=1)     # no crash, no KeyError
    assert rc == 0
    out = capsys.readouterr().out
    assert "incomplete sidecar" in out
    assert "unreadable sidecar" in out


# ---- mirror delete --------------------------------------------------------------

def test_mirror_delete(tmp_path):
    target = tmp_path / "t"
    target.mkdir()
    keep = target / "keep.txt"
    keep.write_text("k")
    gone = target / "gone.txt"
    gone.write_text("g")
    part = target / "keep.txt.part"
    part.write_text("p")
    empty_remote = target / "emptydir"
    empty_remote.mkdir()
    empty_local = target / "localonlydir"
    empty_local.mkdir()
    expected = {os.path.abspath(str(keep)), os.path.abspath(str(empty_remote))}
    deleted = _mirror_delete(str(target), expected)
    assert deleted == 1
    assert keep.exists() and not gone.exists()
    assert part.exists()                        # transient files never touched
    assert empty_remote.exists()                # empty ON THE REMOTE: kept
    assert not empty_local.exists()             # local-only empty dir: pruned


# ---- run_many accounting -----------------------------------------------------------

def test_run_many_accounting():
    tasks = [{"size": 10, "n": "a"}, {"size": 20, "n": "b"},
             {"size": 30, "n": "c"}]

    def work(task, reporter, log_fn, prog, slot):
        if task["n"] == "b":
            raise SynoError({"code": 408})
        return "downloaded"

    stats, total, failures = run_many(tasks, work, jobs=2,
                                      label_of=lambda t: t["n"])
    assert stats["downloaded"] == 2
    assert stats["error"] == 1
    assert total == 40                          # only moved bytes count
    assert len(failures) == 1
    assert failures[0][0] == "error" and failures[0][1] == "b"
    assert "408" in failures[0][2]


def test_run_many_cancels_when_interrupted():
    synofs._interrupt.set()
    tasks = [{"size": 1, "n": "a"}, {"size": 1, "n": "b"}]
    stats, total, failures = run_many(
        tasks, lambda *a: pytest.fail("must not run"), jobs=1,
        label_of=lambda t: t["n"])
    assert stats["cancelled"] == 2
    assert total == 0 and failures == []


# ---- sync: empty dirs + interrupt-safe --delete ------------------------------------

class _StubWalkSyno(Synology):
    """walk() yields canned entries; dirs are reported through the dirs list."""

    def __init__(self, files=(), dirs=()):
        super().__init__("https://nas.example")
        self.files, self.dirs = list(files), list(dirs)

    def walk(self, folder_path, dirs=None):
        if dirs is not None:
            dirs.extend(self.dirs)
        yield from self.files


def _no_transfer(monkeypatch):
    monkeypatch.setattr(synofs, "_download_many",
                        lambda s, t, j, r, **k: (new_stats(), 0, []))


def test_sync_dir_creates_empty_remote_dirs(tmp_path, monkeypatch):
    _no_transfer(monkeypatch)
    f = {"path": "/r/f.bin", "name": "f.bin", "isdir": False,
         "size": 5, "mtime": 1}
    d = {"path": "/r/empty", "name": "empty", "isdir": True, "size": 0,
         "mtime": 0}
    synofs._sync_dir(_StubWalkSyno([f], [d]), "/r", str(tmp_path))
    assert (tmp_path / "r" / "empty").is_dir()


def test_sync_dir_delete_runs_when_walk_completed(tmp_path, monkeypatch):
    _no_transfer(monkeypatch)
    calls = []
    monkeypatch.setattr(synofs, "_mirror_delete",
                        lambda target, expected: calls.append(target) or 0)
    f = {"path": "/r/f.bin", "name": "f.bin", "isdir": False,
         "size": 5, "mtime": 1}
    synofs._sync_dir(_StubWalkSyno([f]), "/r", str(tmp_path), delete=True)
    assert len(calls) == 1


def test_sync_dir_delete_skipped_when_interrupted(tmp_path, monkeypatch):
    _no_transfer(monkeypatch)
    calls = []
    monkeypatch.setattr(synofs, "_mirror_delete",
                        lambda target, expected: calls.append(target) or 0)
    f = {"path": "/r/f.bin", "name": "f.bin", "isdir": False,
         "size": 5, "mtime": 1}
    synofs._interrupt.set()                     # Ctrl-C during the walk
    synofs._sync_dir(_StubWalkSyno([f]), "/r", str(tmp_path), delete=True)
    assert calls == []                          # mirror-delete must NOT run


# ---- upload: symlinks, target creation -----------------------------------------

class _UploadSyno(Synology):
    def __init__(self):
        super().__init__("https://nas.example")
        self.mkdir_calls = []

    def stat(self, path):
        return None

    def walk(self, folder_path, dirs=None):
        return iter(())

    def mkdir(self, path, force_parent=True):
        self.mkdir_calls.append(path)
        return True


def _upload_args(local, remote="/share", **over):
    base = dict(local=local, remote=remote, max_name_bytes=240,
                truncate_names=False, jobs=1, retries=1, verify=False,
                checksum=False, delete=False, yes=False)
    base.update(over)
    return argparse.Namespace(**base)


def test_cmd_upload_skips_symlinks_and_logs(tmp_path, monkeypatch, caplog):
    src = tmp_path / "photos"
    (src / "sub").mkdir(parents=True)
    (src / "a.jpg").write_bytes(b"aa")
    (src / "sub" / "b.jpg").write_bytes(b"bb")
    os.symlink(str(src / "a.jpg"), str(src / "link.jpg"))
    os.symlink(str(src / "sub"), str(src / "dlink"))

    seen = []

    def fake_upload_many(s, tasks, *a, **k):
        seen.extend(tasks)
        return new_stats(), 0, []

    monkeypatch.setattr(synofs, "_upload_many", fake_upload_many)
    syno = _UploadSyno()
    with caplog.at_level(logging.INFO, logger="synofs"):
        rc = synofs.cmd_upload(syno, _upload_args(str(src) + "/"))
    assert rc == 0
    assert sorted(t["name"] for t in seen) == ["a.jpg", "b.jpg"]
    assert all("link" not in t["name"] for t in seen)
    assert syno.mkdir_calls == ["/share"]       # contents-only: target root
    assert "symlink" in caplog.text.lower()     # and the skip was logged


def test_cmd_upload_too_long_without_truncate(tmp_path, monkeypatch, capsys,
                                              caplog):
    src = tmp_path / "d"
    src.mkdir()
    # Locally creatable (<255 bytes) but over the tool's threshold (50 here).
    long_name = "ñ" * 49 + ".txt"               # 102 UTF-8 bytes > 50
    (src / long_name).write_bytes(b"x")
    monkeypatch.setattr(synofs, "_upload_many",
                        lambda *a, **k: (new_stats(), 0, []))
    syno = _UploadSyno()
    with caplog.at_level(logging.WARNING, logger="synofs"):
        rc = synofs.cmd_upload(syno, _upload_args(str(src) + "/",
                                                  max_name_bytes=50))
    assert rc == 1                              # toolong is a failure exit code
    out = capsys.readouterr()
    assert "name too long" in out.out           # end-of-run failure report
    assert "102 bytes" in out.err               # per-file stderr detail
    assert "name too long" in caplog.text       # planning-phase warning


# ---- review round 2: adversarial findings ----------------------------------

def test_finish_partials_non_dict_sidecar(tmp_path, capsys):
    d = tmp_path / "scan"
    d.mkdir()
    (d / "a.part.json").write_text("[1, 2, 3]")   # valid JSON, but not a sidecar
    rc = _finish_partials(None, str(d), retries=1)
    assert rc == 0                                # no AttributeError traceback
    assert "not a synofs sidecar" in capsys.readouterr().out


def test_stream_once_html_with_200_never_touches_part(tmp_path, monkeypatch):
    syno = _dl_syno()
    resp = FakeStreamResp(200, "text/html", b"<html>portal login page</html>")
    monkeypatch.setattr(syno, "call_stream", lambda *a, **k: resp)
    part = tmp_path / "f.bin.part"
    part.write_bytes(b"x" * 30)
    meta = tmp_path / "f.bin.part.json"
    meta.write_text("{}")
    with pytest.raises(SynoError):
        syno._stream_once("/r/f.bin", str(part), str(meta), 100, 0, None)
    assert part.read_bytes() == b"x" * 30         # the portal HTML never entered
    assert meta.exists()
    assert resp.closed


def test_mirror_delete_stops_on_interrupt(tmp_path):
    target = tmp_path / "t"
    target.mkdir()
    (target / "a.txt").write_text("a")
    (target / "b.txt").write_text("b")
    synofs._interrupt.set()                       # Ctrl-C during the delete pass
    deleted = _mirror_delete(str(target), set())
    assert deleted == 0
    assert (target / "a.txt").exists() and (target / "b.txt").exists()


def test_remote_mirror_delete_eof_aborts(monkeypatch):
    syno = _UploadSyno()
    deleted = []
    monkeypatch.setattr(syno, "delete", deleted.append)

    def boom(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", boom)
    n, errors = synofs._remote_mirror_delete(
        syno, {"/share/x.bin": {}}, set(), assume_yes=False)
    assert (n, errors) == (0, [])
    assert deleted == []                          # EOF defaults to NO


def test_cmd_upload_toolong_protects_nas_copy_from_delete(tmp_path, monkeypatch):
    src = tmp_path / "d"
    src.mkdir()
    long_name = "ñ" * 49 + ".txt"                 # 102 UTF-8 bytes > 50
    (src / long_name).write_bytes(b"x")
    truncated = fit_name(long_name, 50)
    remote_trunc = "/share/" + truncated
    monkeypatch.setattr(synofs, "_upload_many",
                        lambda *a, **k: (new_stats(), 0, []))

    class IndexSyno(_UploadSyno):
        def __init__(self):
            super().__init__()
            self.deleted = []

        def stat(self, path):
            if path == "/share":
                return {"path": path, "name": "share", "isdir": True,
                        "size": 0, "mtime": 0}
            return None

        def walk(self, folder_path, dirs=None):
            # the NAS holds the file under its TRUNCATED name (an earlier
            # --truncate-names run put it there)
            yield {"path": remote_trunc, "name": truncated,
                   "isdir": False, "size": 1, "mtime": 0}

        def delete(self, path):
            self.deleted.append(path)

    syno = IndexSyno()
    rc = synofs.cmd_upload(syno, _upload_args(str(src) + "/", max_name_bytes=50,
                                              delete=True, yes=True))
    assert rc == 1                # toolong still counts as a failure, but ...
    assert syno.deleted == []     # ... the NAS copy of a local file SURVIVES


def test_cmd_upload_failed_nas_delete_sets_exit_code(tmp_path, monkeypatch):
    src = tmp_path / "d"
    src.mkdir()                                   # empty local tree
    monkeypatch.setattr(synofs, "_upload_many",
                        lambda *a, **k: (new_stats(), 0, []))

    class DelSyno(_UploadSyno):
        def stat(self, path):
            return {"path": path, "name": os.path.basename(path),
                    "isdir": True, "size": 0, "mtime": 0}

        def walk(self, folder_path, dirs=None):
            yield {"path": "/share/old.bin", "name": "old.bin",
                   "isdir": False, "size": 1, "mtime": 0}

        def delete(self, path):
            raise SynoError({"code": 403})        # permission denied mid-pass

    rc = synofs.cmd_upload(DelSyno(), _upload_args(str(src) + "/",
                                                   delete=True, yes=True))
    assert rc == 1                                # a failed delete is not clean
