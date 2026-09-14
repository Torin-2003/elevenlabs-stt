#!/usr/bin/env python3
"""Offline self-check for elevenlabs-stt (no network).

Entry points: `python stt.py selfcheck` (delegates here) or `python selfcheck.py`.
Pure asserts over stt.py + audio_split.py logic; mutated stt globals
(_GATE_NEXT / *_LOG hooks / ACCOUNTS_PATH) are set via module attributes.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import sys
import tempfile
import time

import audio_split
import proxy
import stt
from stt import (
    DEFAULTS, _rlog, _session_to_account, _stagger_wait, _tlog, accounts_config,
    allocate, cached_remaining, filter_accounts, fresh_count, group_plan,
    load_config, pack_bins, random_password, resolve_language,
    run_plan_pipelined, temp_email_config,
)


def run() -> int:
    """Minimal runnable self-check (no network)."""
    cfg = load_config(pathlib.Path("config.example.toml"))
    assert cfg["include_subtitles"] is True, "script default include_subtitles must be ON"
    assert cfg["tag_audio_events"] is True
    assert cfg["max_concurrency"] == 4 and cfg["stagger_secs"] == 2.0  # 并发默认值（AC4）
    assert DEFAULTS["max_concurrency"] == 4 and DEFAULTS["stagger_secs"] == 2.0
    assert resolve_language("auto") is None
    assert resolve_language("English") == "eng"
    assert resolve_language("eng") == "eng"
    assert resolve_language("zho") == "zho"
    # multipart field builder sanity (no upload)
    opts = dict(cfg); opts["language_code"] = None; opts["vocab"] = ["a", "b"]
    data = {"task_name": "x", "model_id": "scribe_v2",
            "tag_audio_events": "true", "include_subtitles": "true", "keyterms": ["a", "b"]}
    assert data["keyterms"] == ["a", "b"]
    # multi-account config sections + migration shape
    tcfg = temp_email_config(pathlib.Path("config.example.toml"))
    assert tcfg["poll_interval_secs"] == 3 and tcfg["use_admin_path"] is True
    acfg = accounts_config(pathlib.Path("config.example.toml"))
    assert acfg["selection_margin"] == 1.2 and acfg["pool_target"] == 3
    acct = _session_to_account(
        {"email": "a@b.c", "refreshToken": "rt", "localId": "uid", "jwt": "j", "jwt_exp": 0},
        source="manual")
    assert acct["source"] == "manual" and acct["email"] == "a@b.c" and acct["invalid"] is False
    # selection: best-fit + margin (offline via fresh credits_known cache; no network)
    now = time.time()
    fake = [
        {"email": "a", "invalid": False, "credits_known": {"limit": 10000, "count": 9000, "fetched_at": now}},
        {"email": "b", "invalid": False, "credits_known": {"limit": 10000, "count": 0, "fetched_at": now}},
        {"email": "c", "invalid": True,  "credits_known": {"limit": 10000, "count": 0, "fetched_at": now}},
    ]
    fstore = {"accounts": fake, "active": "a"}
    assert cached_remaining(fake[0]) == 1000 and cached_remaining(fake[1]) == 10000
    pw = random_password()
    assert len(pw) >= 8 and any(c.isdigit() for c in pw) and any(not c.isalnum() for c in pw)
    assert fresh_count(fstore, 10000) == 1 and fresh_count(fstore, 1000) == 2
    # allocate: bin-packing best-fit, existing-first, exact shortfall (offline via credits_known)
    aa = {"email": "a", "invalid": False, "credits_known": {"limit": 1000, "count": 0, "fetched_at": now}}
    # b sized so 7000 can't fit it (forces a NEW bin) while 5000+400 do (design mapping).
    bb = {"email": "b", "invalid": False, "credits_known": {"limit": 6000, "count": 0, "fetched_at": now}}
    astore = {"accounts": [aa, bb], "active": "a"}
    # margin 1.0 so need == required; files needing [800, 5000, 7000, 400]
    plan, reg = allocate([("f800", 800), ("f5000", 5000), ("f7000", 7000), ("f400", 400)],
                         [aa, bb], margin=1.0, fresh_threshold=10000, store=astore)
    pd = {f: acct for f, acct in plan}
    # 7000 fits no existing account (a=1000, b=6000) -> its own fresh bin, exactly one shortfall.
    assert pd["f7000"] == "NEW#0", "7000 -> fresh bin"
    assert reg == 1, f"register_count should be 1, got {reg}"
    # the three that fit are packed onto existing accounts (best-fit, existing-first).
    assert all(pd[f] in (aa, bb) for f in ("f800", "f5000", "f400")), "small files packed onto existing"
    # no account over-committed beyond its remaining (with margin 1.0, need == required).
    used = {"a": 0, "b": 0}
    for f, req in [("f800", 800), ("f5000", 5000), ("f400", 400)]:
        used[pd[f]["email"]] += req
    assert used["a"] <= 1000 and used["b"] <= 6000, f"over-commit: {used}"
    # existing-account files ordered before the NEW file (AC3)
    new_idx = next(i for i, (f, acct) in enumerate(plan) if isinstance(acct, str))
    assert all(not isinstance(acct, str) for f, acct in plan[:new_idx]), "existing before NEW"
    # unknown-duration file -> own fresh bin, increments register_count
    plan2, reg2 = allocate([("fu", None)], [aa, bb], margin=1.0, fresh_threshold=10000, store=astore)
    assert reg2 == 1 and plan2[0][1] == "NEW#0", "unknown duration -> own NEW bin"
    # a single file bigger than one fresh account -> out-of-scope guard raises
    try:
        allocate([("big", 20000)], [aa, bb], margin=1.0, fresh_threshold=10000, store=astore)
        assert False, "oversize file should raise"
    except SystemExit:
        pass
    # pack_bins: 纯核心与 allocate 同规则；oversize 收集而非 raise
    r = pack_bins([("f800", 800), ("f5000", 5000), ("f7000", 7000), ("f400", 400)],
                  [("a", 1000), ("b", 6000)], margin=1.0, fresh_threshold=10000)
    assert r["register_count"] == 1 and dict(r["assignments"])["f7000"] == "NEW#0"
    assert [b["is_new"] for b in r["bins"]] == sorted(b["is_new"] for b in r["bins"]), \
        "existing bins before NEW bins"
    for b in r["bins"]:
        assert b["is_new"] or b["use"] <= b["before"], f"over-commit: {b}"
    r2 = pack_bins([("big", 20000)], [("a", 1000)], 1.0, 10000)
    assert r2["oversize"] == [("big", 20001)] and r2["assignments"] == []
    r3 = pack_bins([("fu", None)], [("a", 1000)], 1.0, 10000)
    assert r3["register_count"] == 1 and dict(r3["assignments"])["fu"] == "NEW#0"
    assert r3["bins"][-1]["use"] == 0 and r3["bins"][-1]["before"] == 0  # unknown req 不计 use
    # --- 并发流水线纯逻辑（AC7；全部离线） -------------------------------------
    # group_plan: existing 按账号聚组保序、NEW#k 按槽位分组
    p1, p2, p3 = (pathlib.Path(n) for n in ("p1.mp3", "p2.mp3", "p3.mp3"))
    egroups, pending = group_plan([(p1, aa), (p2, bb), (p3, aa), (pathlib.Path("p4.mp3"), "NEW#0")])
    assert [(acct["email"], [i for i, _ in items]) for acct, items in egroups] == \
        [("a", [0, 2]), ("b", [1])], "同账号聚为一组且保 plan 序"
    assert {k: [i for i, _ in v] for k, v in pending.items()} == {0: [3]}

    # stagger 闸：0 短路；两次取号间隔 >= stagger_secs（注入时钟，无真实 sleep）
    stt._GATE_NEXT = 0.0
    assert _stagger_wait(0) == 0.0
    slept: list[float] = []
    w1 = _stagger_wait(2.0, clock=lambda: 100.0, sleep=slept.append)
    w2 = _stagger_wait(2.0, clock=lambda: 100.0, sleep=slept.append)
    assert w1 == 0.0 and w2 == 2.0 and slept == [0.0, 2.0], (w1, w2, slept)
    stt._GATE_NEXT = 0.0

    # run_plan_pipelined: 假账号 + transcribe 桩；注册第 2 个失败 → 该槽位 FAIL，
    # 已提交组与已注册槽位不受影响；results 保 plan 序（R7 流水线新分支）
    real_accounts_path = stt.ACCOUNTS_PATH
    stt.ACCOUNTS_PATH = pathlib.Path(tempfile.mkdtemp(prefix="stt-selfcheck-")) / "accounts.json"
    try:
        reg_calls: list[int] = []

        def reg_stub():
            reg_calls.append(1)
            if len(reg_calls) == 1:
                return {"email": "n0", "invalid": False, "created_at": now,
                        "credits_known": {"limit": 10000, "count": 0, "fetched_at": now}}
            raise RuntimeError("boom")

        def trans_stub(inp, _cfg, account, _store, _cfgpath, output):
            return pathlib.Path(str(inp) + ".srt")

        pstore = {"accounts": [dict(aa)], "active": "a"}
        pplan = [(p1, pstore["accounts"][0]), (p2, "NEW#0"), (p3, "NEW#1")]
        done_flags: list[bool] = []
        plog: list[str] = []
        stt.TRANSCRIBE_LOG = stt.REGISTER_LOG = plog.append  # 静音测试期间的进度行
        try:
            res, pused = run_plan_pipelined(
                pplan, {"max_concurrency": 2, "stagger_secs": 0}, pstore,
                pathlib.Path("config.example.toml"), output_for=lambda _i: None,
                register_count=2, register_fn=reg_stub, transcribe_fn=trans_stub,
                refresh_used=False, on_done=lambda _i, ok: done_flags.append(ok))
        finally:
            stt.TRANSCRIBE_LOG = stt.REGISTER_LOG = None
        assert any("注册失败" in l for l in plog), plog
        assert [r[0] for r in res] == [p1, p2, p3], "results 按 plan 原顺序聚合"
        assert res[0][2] == "OK" and res[1][2] == "OK" and res[1][1] == "n0", res
        assert res[2][2] == "FAIL" and "注册失败" in res[2][3], "未注册槽位标 FAIL 带原因"
        assert {a["email"] for a in pstore["accounts"]} == {"a", "n0"}, "注册成功的账号已入 store"
        assert stt.ACCOUNTS_PATH.exists(), "注册成功即落盘"
        assert len(done_flags) == 3 and done_flags.count(False) == 1, done_flags
        assert {a.get("email") for a in pused} == {"a", "n0"}

        # --- A1 atomic write + A2 pending recovery (AC1-AC9) -----------------
        # Isolate on a fresh file in the same tempdir so the outer rmtree still covers it.
        a1a2_path = stt.ACCOUNTS_PATH.parent / "a1a2-accounts.json"
        saved_path = stt.ACCOUNTS_PATH
        stt.ACCOUNTS_PATH = a1a2_path
        try:
            # AC1: _write_accounts is atomic — valid JSON, no .tmp lingers on success
            st = {"accounts": [], "active": None}
            stt._write_accounts(st)
            assert json.load(open(stt.ACCOUNTS_PATH, encoding="utf-8")) == st, "atomic write content"
            assert not list(stt.ACCOUNTS_PATH.parent.glob("a1a2-*.tmp")), "tmp file leaked"
            # AC2: save failure salvages to pending and raises SystemExit(remedy)
            acct2 = {"email": "salv@x.y", "invalid": False, "created_at": 0.0,
                     "refreshToken": "rt"}
            orig_w = stt._write_accounts
            def _boom(_s):
                raise OSError("disk full")
            stt._write_accounts = _boom
            try:
                stt.persist_registered(st, acct2)
                assert False, "persist_registered should have raised SystemExit"
            except SystemExit:
                pass
            finally:
                stt._write_accounts = orig_w
            assert stt._pending_path().exists(), "pending jsonl written on salvage"
            assert "salv@x.y" in stt._pending_path().read_text(encoding="utf-8"), "pending has email"
            assert any(a.get("email") == "salv@x.y" for a in st["accounts"]), "salvaged account upserted in-memory"
            # AC5/AC6: recover_pending folds back, persists, removes pending; idempotent
            st_back = {"accounts": [], "active": None}
            r = stt.recover_pending(st_back)
            assert r["restored"] >= 1 and r["skipped"] == 0, r
            assert not stt._pending_path().exists(), "pending removed after successful recover"
            assert any(a.get("email") == "salv@x.y" for a in st_back["accounts"]), "recovered into store"
            assert json.load(open(stt.ACCOUNTS_PATH, encoding="utf-8"))["accounts"][0]["email"] == "salv@x.y", "recovered onto disk"
            r2 = stt.recover_pending(st_back)
            assert r2 == {"restored": 0, "skipped": 0}, r2
            # AC9: keep_active=True preserves store["active"] in the saved file
            st_keep = {"accounts": [{"email": "old@x.y", "invalid": False, "created_at": 0.0}],
                       "active": "old@x.y"}
            new_acct = {"email": "new@x.y", "invalid": False, "created_at": 0.0, "refreshToken": "rt"}
            stt.persist_registered(st_keep, new_acct, keep_active=True)
            assert st_keep["active"] == "old@x.y", f"keep_active restored active: {st_keep['active']}"
            assert json.load(open(stt.ACCOUNTS_PATH, encoding="utf-8"))["active"] == "old@x.y", "saved file keeps old active"
            assert any(a.get("email") == "new@x.y" for a in st_keep["accounts"]), "new account upserted under keep_active"
            # default keep_active=False leaves the new account active (matches run_register/cmd_pool_warm/web)
            st_def = {"accounts": [], "active": None}
            stt.persist_registered(st_def, new_acct)
            assert st_def["active"] == "new@x.y", f"default keep_active left new active: {st_def['active']}"
            assert json.load(open(stt.ACCOUNTS_PATH, encoding="utf-8"))["active"] == "new@x.y", "default saved new active"
        finally:
            stt.ACCOUNTS_PATH = saved_path
    finally:
        shutil.rmtree(stt.ACCOUNTS_PATH.parent, ignore_errors=True)
        stt.ACCOUNTS_PATH = real_accounts_path

    # --- audio_split pure logic (offline; AC7) --------------------------------
    # default_chunk_secs: chunk fits one fresh account within margin
    assert audio_split.default_chunk_secs(10000, 13.9, 1.2) == 569
    # timestamp parse/fmt round-trip (srt comma-ms, vtt dot-ms)
    assert audio_split.fmt_ts_srt(3661.5) == "01:01:01,500"
    assert audio_split.fmt_ts_vtt(3661.5) == "01:01:01.500"
    assert abs(audio_split.parse_ts_srt("01:01:01,500") - 3661.5) < 1e-6
    assert abs(audio_split.parse_ts_vtt("01:01:01.500") - 3661.5) < 1e-6
    # plan_cuts: latest silence midpoint in window; MIN_SEG(5s) filters near-start cand
    segs, hard = audio_split.plan_cuts(500.0, 300.0, [3.0, 100.0, 250.0, 280.0])
    assert segs == [(0.0, 280.0), (280.0, 500.0)], segs  # 280 = latest cand <= 300; 3 filtered
    assert hard == [False, False]
    assert all(e - s <= 300.0 + 1e-9 for s, e in segs)
    # plan_cuts hard-cut fallback when no silence candidate
    segs2, hard2 = audio_split.plan_cuts(700.0, 300.0, [])
    assert segs2 == [(0.0, 300.0), (300.0, 600.0), (600.0, 700.0)], segs2
    assert hard2 == [True, True, False]
    # plan_cuts_skip: silences >= skip_min are dropped, short ones untouched (AC1)
    sil = [(50.0, 65.0), (200.0, 201.0), (400.0, 412.0)]  # 15s + 1s + 12s, skip_min=10
    sk, skh = audio_split.plan_cuts_skip(500.0, 300.0, sil, 10.0)
    pad = audio_split.SKIP_EDGE_PAD
    # middle voiced region (64.5..400.5 = 336s > 300) greedy-splits at the 1s-silence midpoint
    assert sk == [(0.0, 50.0 + pad), (65.0 - pad, 200.5), (200.5, 400.0 + pad),
                  (412.0 - pad, 500.0)], sk
    assert len(skh) == len(sk)
    for a, b in sk:  # every segment <= chunk_secs, absolute coords inside [0, total]
        assert 0.0 <= a < b <= 500.0 and b - a <= 300.0 + 1e-9, (a, b)
    for s, e in [(50.0, 65.0), (400.0, 412.0)]:  # long silences fully skipped (pad aside)
        assert not any(a < (s + e) / 2 < b for a, b in sk), (s, e, sk)
    skipped_total = 500.0 - sum(b - a for a, b in sk)
    assert abs(skipped_total - (15.0 + 12.0 - 4 * pad)) < 1e-6, skipped_total
    # >= boundary: a silence exactly skip_min long is skipped
    skb, _ = audio_split.plan_cuts_skip(100.0, 300.0, [(40.0, 50.0)], 10.0)
    assert skb == [(0.0, 40.0 + pad), (50.0 - pad, 100.0)], skb
    # no long silence: byte-identical to plan_cuts on the midpoints
    same = audio_split.plan_cuts_skip(500.0, 300.0, [(99.0, 101.0), (249.0, 251.0), (279.0, 281.0)], 10.0)
    assert same == audio_split.plan_cuts(500.0, 300.0, [100.0, 250.0, 280.0]), same
    # all-silence audio -> empty plan; leading/trailing long silence -> degenerate dropped
    assert audio_split.plan_cuts_skip(60.0, 300.0, [(0.0, 60.0)], 10.0) == ([], [])
    ends, _ = audio_split.plan_cuts_skip(100.0, 300.0, [(0.0, 20.0), (80.0, 100.0)], 10.0)
    assert ends == [(20.0 - pad, 80.0 + pad)], ends
    # long voiced region still greedy-splits on the short silences inside it
    lsil = [(0.0, 30.0), (250.0, 251.0)]
    lsk, lskh = audio_split.plan_cuts_skip(700.0, 300.0, lsil, 10.0)
    assert all(b - a <= 300.0 + 1e-9 for a, b in lsk), lsk
    assert lsk[0] == (30.0 - pad, 250.5), lsk  # cut at the short-silence midpoint
    # merge_srt: offset correction + start-sort + contiguous renumber from 1
    c0 = "1\n00:00:00,000 --> 00:00:01,000\nhello\n"
    c1 = "1\n00:00:00,500 --> 00:00:01,500\nworld\n"
    m = audio_split.merge_srt([(0.0, c0), (100.0, c1)])
    assert m.startswith("1\n"), m
    assert "\n2\n" in m and m.count("-->") == 2
    assert "00:01:40,500 --> 00:01:41,500" in m  # world offset by 100s
    assert "hello" in m and "world" in m
    # merge_vtt: single WEBVTT header, offset + renumber
    v0 = "WEBVTT\n\ncue-1\n00:00:00.000 --> 00:00:01.000\nhi\n"
    v1 = "WEBVTT\n\ncue-1\n00:00:02.000 --> 00:00:03.000\nbye\n"
    mv = audio_split.merge_vtt([(0.0, v0), (10.0, v1)])
    assert mv.count("WEBVTT") == 1, mv
    assert "00:00:12.000 --> 00:00:13.000" in mv  # bye offset by 10s
    assert "\n1\n" in mv and "\n2\n" in mv
    # merge_txt: segment-order concatenation
    assert audio_split.merge_txt([(0.0, "alpha"), (5.0, "beta")]) == "alpha\n\nbeta\n"

    # video filename probe (web upload routing; no ffmpeg)
    assert audio_split.is_video_filename("clip.MP4") and audio_split.is_video_filename("a.mkv")
    assert not audio_split.is_video_filename("talk.m4a") and not audio_split.is_video_filename("x")
    assert audio_split.VIDEO_EXTENSIONS >= {".mp4", ".webm", ".mov"}

    # extract_audio: short synthetic mp4 → audio (skip if ffmpeg/ffprobe unavailable)
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        tdir = pathlib.Path(tempfile.mkdtemp(prefix="stt-extract-"))
        try:
            vid = tdir / "clip.mp4"
            # lavfi sine + color: portable short A/V fixture
            subprocess_ok = __import__("subprocess").run(
                [shutil.which("ffmpeg"), "-y",
                 "-f", "lavfi", "-i", "sine=frequency=440:duration=0.4",
                 "-f", "lavfi", "-i", "color=c=black:s=160x120:d=0.4",
                 "-c:a", "aac", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                 "-shortest", str(vid)],
                stdout=__import__("subprocess").DEVNULL,
                stderr=__import__("subprocess").DEVNULL,
            )
            if subprocess_ok.returncode == 0 and vid.is_file():
                er = audio_split.extract_audio(vid, tdir, "out")
                assert er.method in ("copy", "transcode"), er
                assert er.path.is_file() and er.path.stat().st_size > 0
                assert er.path.suffix.lower() in (".mka", ".m4a")
                assert vid.is_file()  # helper must not delete src
        finally:
            shutil.rmtree(tdir, ignore_errors=True)

    # REGISTER_LOG hook: collector receives _rlog lines; None default restored
    got: list[str] = []
    stt.REGISTER_LOG = got.append
    try:
        _rlog("hook-test")
    finally:
        stt.REGISTER_LOG = None
    assert got == ["hook-test"], got

    # TRANSCRIBE_LOG hook: same contract as REGISTER_LOG
    tgot: list[str] = []
    stt.TRANSCRIBE_LOG = tgot.append
    try:
        _tlog("t-hook-test")
    finally:
        stt.TRANSCRIBE_LOG = None
    assert tgot == ["t-hook-test"], tgot

    # filter_accounts: empty emails → all non-invalid; manual limits; unknown raises
    cand, manual = filter_accounts(fstore, [])
    assert manual is False and [a["email"] for a in cand] == ["a", "b"]
    cand, manual = filter_accounts(fstore, ["b"])
    assert manual is True and [a["email"] for a in cand] == ["b"]
    try:
        filter_accounts(fstore, ["b", "nobody@x.y"])
        assert False, "unknown email should raise"
    except SystemExit as e:
        assert "nobody@x.y" in str(e)
    try:
        filter_accounts(fstore, ["c"])  # invalid account is not a candidate
        assert False, "invalid email should raise"
    except SystemExit:
        pass

    _check_proxy_driver()
    _check_email_provider()
    _check_site_password_headers()
    _check_register_dispatch()
    _check_register_orchestration()
    _check_mac_chrome_discovery()
    _check_proxy_threading()

    print("selfcheck ok")
    return 0


def _check_proxy_driver() -> None:
    now = time.time()
    # sticky-session substitution: {session} → fresh id per call; else unchanged
    assert proxy.with_session(None) is None
    assert proxy.with_session("http://u:p@g:1") == "http://u:p@g:1"
    s1 = proxy.with_session("http://u-session-{session}:p@g:1")
    assert "{session}" not in s1 and s1 != "http://u-session-{session}:p@g:1"
    assert proxy.with_session("http://x-{session}@g") != proxy.with_session("http://x-{session}@g")

    # empty pool → always direct; has_proxies distinguishes "none" from "all disabled"
    d = proxy.ProxyDriver({"proxies": [], "fail_threshold": 3, "cooldown_secs": 900, "strict": False})
    assert d.pick() is None, "empty pool must yield direct connection"
    assert d.has_proxies is False, "empty pool has no proxies"
    assert proxy.ProxyDriver({"proxies": ["http://a"]}).has_proxies is True

    # round-robin over two live proxies
    cfg = {"proxies": ["http://a", "http://b"], "fail_threshold": 2, "cooldown_secs": 900, "strict": False}
    d = proxy.ProxyDriver(cfg)
    p1, p2, p3 = d.pick(), d.pick(), d.pick()
    assert {p1.url, p2.url} == {"http://a", "http://b"}, "round-robin should cover both"
    assert p3.url == p1.url, "cursor wraps around"

    # fail to threshold → disabled; only the other remains
    d = proxy.ProxyDriver(cfg)
    bad = next(p for p in (d.pick(), d.pick()) if p.url == "http://a")
    d.mark_fail(bad); d.mark_fail(bad)  # 2 hits threshold
    assert {d.pick().url for _ in range(4)} == {"http://b"}, "disabled proxy must drop out"

    # mark_ok resets fail count (1 fail after ok < threshold → still live)
    d = proxy.ProxyDriver(cfg)
    a = next(p for p in (d.pick(), d.pick()) if p.url == "http://a")
    d.mark_fail(a); d.mark_ok(a); d.mark_fail(a)
    assert "http://a" in {d.pick().url for _ in range(4)}, "mark_ok must reset fail count"

    # sole disabled proxy → direct (strict=False); cooldown expiry revives
    d = proxy.ProxyDriver({"proxies": ["http://a"], "fail_threshold": 1, "cooldown_secs": 900, "strict": False})
    a = d.pick(); d.mark_fail(a)
    assert d.pick() is None, "sole disabled proxy + strict=False → direct"
    a.disabled_until = now - 1  # manual expiry
    assert d.pick().url == "http://a", "cooldown expiry must revive proxy"

    # strict=True, all disabled → raise
    d = proxy.ProxyDriver({"proxies": ["http://a"], "fail_threshold": 1, "cooldown_secs": 900, "strict": True})
    a = d.pick(); d.mark_fail(a)
    try:
        d.pick()
        assert False, "strict pool exhausted must raise"
    except SystemExit as e:
        assert "strict" in str(e)


def _check_email_provider() -> None:
    import httpx
    import register

    # VERIFY_LINK_PATTERN contract: only elevenlabs.io action links
    good = 'see https://elevenlabs.io/app/action?mode=verifyEmail&oobCode=XYZ&x=1 end'
    bad = 'https://evil.com/app/action?oobCode=XYZ'
    assert register.VERIFY_LINK_PATTERN.search(good).group(0).endswith("x=1")
    assert register.VERIFY_LINK_PATTERN.search(bad) is None

    # constructs from cfg without touching the network
    cfg = temp_email_config(pathlib.Path("config.example.toml"))
    prov = register.CloudflareTempEmail(cfg)
    assert hasattr(prov, "create_address") and hasattr(prov, "poll_verification_link")

    # domain rotation: capture the "domain" posted across create_address() calls
    posted: list[str] = []

    class _Resp:
        status_code = 200
        def json(self):
            return {"address": f"u@{posted[-1]}", "jwt": "tok"}

    class _Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, json=None, headers=None):
            posted.append(json["domain"])
            return _Resp()

    base = {"base_url": "https://m.x", "admin_password": "", "site_password": "",
            "use_admin_path": True}
    orig = httpx.Client
    httpx.Client = _Client
    try:
        multi = register.CloudflareTempEmail(
            {**base, "domain": "a.com", "domains": ["a.com", "b.com", "c.com"]})
        for _ in range(4):
            multi.create_address()
        assert posted == ["a.com", "b.com", "c.com", "a.com"], f"rotate domains, got {posted}"
        posted.clear()
        single = register.CloudflareTempEmail(
            {**base, "domain": "only.com", "domains": ["only.com"]})
        for _ in range(3):
            single.create_address()
        assert posted == ["only.com"] * 3, f"single domain stays constant, got {posted}"
    finally:
        httpx.Client = orig

    # a fake provider duck-types for the strategy
    class FakeProvider:
        def create_address(self):
            return register.EmailAddress(address="x@t.co", token="jwt", raw={})
        def poll_verification_link(self, addr, pattern, timeout_s, interval_s):
            return "https://elevenlabs.io/app/action?oobCode=Z"
    assert FakeProvider().create_address().address == "x@t.co"


def _check_site_password_headers() -> None:
    import httpx, register
    seen: list = []

    class _Resp:
        status_code = 200
        def __init__(self, payload): self._p = payload
        def json(self): return self._p

    class _Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, json=None, headers=None):
            seen.append(("POST", url, headers or {}))
            return _Resp({"address": "x@for-in.top", "jwt": "tok"})
        def get(self, url, params=None, headers=None):
            seen.append(("GET", url, headers or {}))
            return _Resp({"results": [{"text": "go https://elevenlabs.io/app/action?mode=verifyEmail&oobCode=Z end"}]})

    cfg = {"base_url": "https://m.x", "domain": "for-in.top", "domains": ["for-in.top"],
           "admin_password": "adm", "site_password": "sitepw", "use_admin_path": True}
    orig = httpx.Client
    httpx.Client = _Client
    try:
        prov = register.CloudflareTempEmail(cfg)
        addr = prov.create_address()
        prov.poll_verification_link(addr, register.VERIFY_LINK_PATTERN, 5, 0.1)
        admin = next(h for m, u, h in seen if "/admin/new_address" in u)
        assert admin.get("x-custom-auth") == "sitepw" and admin.get("x-admin-auth") == "adm", admin
        poll = next(h for m, u, h in seen if "/parsed_mails" in u)
        assert poll.get("x-custom-auth") == "sitepw", poll
        # no site_password → no x-custom-auth header
        seen.clear()
        register.CloudflareTempEmail({**cfg, "site_password": ""}).create_address()
        admin2 = next(h for m, u, h in seen if "/admin/new_address" in u)
        assert "x-custom-auth" not in admin2, admin2
    finally:
        httpx.Client = orig


def _check_register_dispatch() -> None:
    import register

    seen = {}

    class FakeStrategy:
        def register(self, *, provider, proxy_driver, captcha=None):
            seen["provider"] = provider
            seen["proxy_driver"] = proxy_driver
            return {"email": "fake@x", "ok": True}

    class FakeProvider:
        def create_address(self): raise AssertionError("not called")
        def poll_verification_link(self, *a, **k): raise AssertionError("not called")

    empty_pool = proxy.ProxyDriver({"proxies": []})
    fp = FakeProvider()
    out = register.register_one(strategy=FakeStrategy(), provider=fp, proxy_driver=empty_pool)
    assert out == {"email": "fake@x", "ok": True}
    assert seen["provider"] is fp and seen["proxy_driver"] is empty_pool, "dispatcher must inject shared services"

    # http stub still raises NotImplementedError; unknown strategy → SystemExit.
    orig = stt.register_config
    try:
        stt.register_config = lambda *a, **k: {"strategy": "http"}
        try:
            register.register_one()
            assert False, "http stub must raise NotImplementedError"
        except NotImplementedError as e:
            assert "ui" in str(e), "stub should point to strategy='ui'"
        stt.register_config = lambda *a, **k: {"strategy": "zzz"}
        try:
            register.register_one()
            assert False, "unknown strategy must raise SystemExit"
        except SystemExit as e:
            assert "zzz" in str(e)
    finally:
        stt.register_config = orig

    # camoufox/cdp factories build a CamoufoxStrategy (don't call register() — it
    # would launch a real browser). Import is browser-dep-free at module load.
    import register_camoufox
    assert isinstance(register._STRATEGIES["camoufox"](), register_camoufox.CamoufoxStrategy)
    assert isinstance(register._STRATEGIES["cdp"](), register_camoufox.CamoufoxStrategy)


def _check_register_orchestration() -> None:
    import register
    calls: list = []

    class FakeWindow:
        left = top = 0
        width = height = 1000

    class FakePlatform:
        mod_key = "ctrl"
        def launch_chrome(self, profile_dir, signup_url, proxy_url):
            calls.append(("launch", proxy_url)); return None
        def find_profile_window(self, profile_dir, timeout_s):
            calls.append("find"); return FakeWindow()
        def ensure_foreground(self, window):
            calls.append("focus")
        def kill_profile(self, profile_dir, popen):
            calls.append("kill"); shutil.rmtree(profile_dir, ignore_errors=True)

    class FakeProvider:
        def create_address(self):
            calls.append("create_addr")
            return register.EmailAddress(address="e@t.co", token="jwt", raw={})
        def poll_verification_link(self, addr, pattern, timeout_s, interval_s):
            calls.append("poll")
            return "https://elevenlabs.io/app/action?oobCode=Z"

    # stub the parts that would touch the real OS / network, and the sleeps
    saved = {n: getattr(register, n) for n in
             ("_fill_signup_form", "_open_verify_link_and_confirm", "_sign_in")}
    saved_stt = {n: getattr(stt, n) for n in
                 ("account_from_password_signin", "authed_client", "refresh_credits",
                  "cached_remaining")}
    saved_sleep = time.sleep
    register._fill_signup_form = lambda *a, **k: calls.append("fill")
    register._open_verify_link_and_confirm = lambda *a, **k: calls.append("verify")
    register._sign_in = lambda *a, **k: calls.append("signin")
    fake_account = {"email": "e@t.co"}
    stt.account_from_password_signin = lambda *a, **k: fake_account

    class _FakeClient:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **k): pass
    stt.authed_client = lambda *a, **k: _FakeClient()
    stt.refresh_credits = lambda *a, **k: None
    stt.cached_remaining = lambda *a, **k: 10000
    time.sleep = lambda *a, **k: None
    saved_reg_log = stt.REGISTER_LOG
    stt.REGISTER_LOG = lambda _m: None
    try:
        # happy path: empty pool → proxy_url None; call order is the contract
        pd = proxy.ProxyDriver({"proxies": []})
        acct = register.UICoordinateStrategy(platform=FakePlatform()).register(
            provider=FakeProvider(), proxy_driver=pd)
        assert acct is fake_account
        order = [c if isinstance(c, str) else c[0] for c in calls]
        assert order == ["create_addr", "launch", "find", "focus", "fill",
                         "poll", "verify", "signin", "kill"], order
        assert ("launch", None) in calls, "empty pool → proxy_url None to Chrome"

        # failure path: poll raises → kill still runs (finally), proxy marked failed
        calls.clear()
        marks: list = []

        class FailProvider(FakeProvider):
            def poll_verification_link(self, *a, **k):
                calls.append("poll"); raise SystemExit("boom")

        pd2 = proxy.ProxyDriver({"proxies": ["http://p"], "fail_threshold": 3,
                                 "cooldown_secs": 900, "strict": False})
        orig_fail = pd2.mark_fail
        pd2.mark_fail = lambda p: (marks.append(p.url), orig_fail(p))
        try:
            register.UICoordinateStrategy(platform=FakePlatform()).register(
                provider=FailProvider(), proxy_driver=pd2)
            assert False, "should propagate"
        except SystemExit:
            pass
        assert "kill" in [c if isinstance(c, str) else c[0] for c in calls], "kill must run in finally"
        assert ("launch", "http://p") in calls, "picked proxy_url must reach launch_chrome"
        assert marks == ["http://p"], "failure must mark_fail the picked proxy"

        # all-proxies-disabled + strict=False → warn and register direct (proxy_url None)
        calls.clear()
        logs: list = []
        stt.REGISTER_LOG = lambda m: logs.append(m)
        pd3 = proxy.ProxyDriver({"proxies": ["http://d"], "fail_threshold": 1,
                                 "cooldown_secs": 900, "strict": False})
        pd3.mark_fail(pd3.pick())  # disable the only proxy
        register.UICoordinateStrategy(platform=FakePlatform()).register(
            provider=FakeProvider(), proxy_driver=pd3)
        assert ("launch", None) in calls, "all-disabled + strict=False → direct"
        assert any("所有代理已禁用" in m for m in logs), "must warn when falling back to direct"
        stt.REGISTER_LOG = lambda _m: None
    finally:
        for n, fn in saved.items():
            setattr(register, n, fn)
        for n, fn in saved_stt.items():
            setattr(stt, n, fn)
        time.sleep = saved_sleep
        stt.REGISTER_LOG = saved_reg_log


def _check_mac_chrome_discovery() -> None:
    import register_platform_mac as mac
    CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    BRAVE = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
    # env override wins
    assert mac._find_chrome_binary({"ELEVENLABS_STT_CHROME": "/custom/x"},
                                   exists=lambda p: True) == "/custom/x"
    # Chrome preferred over Brave when both present
    assert mac._find_chrome_binary({}, exists=lambda p: p in (CHROME, BRAVE)) == CHROME
    # Brave when only Brave present
    assert mac._find_chrome_binary({}, exists=lambda p: p == BRAVE) == BRAVE
    # neither → SystemExit pointing at the override env var
    try:
        mac._find_chrome_binary({}, exists=lambda p: False)
        assert False, "no browser must raise"
    except SystemExit as e:
        assert "ELEVENLABS_STT_CHROME" in str(e)
    # MacDriver uses the command modifier for clipboard shortcuts
    assert mac.MacDriver.mod_key == "command"


def _check_proxy_threading() -> None:
    import httpx
    captured: dict = {}
    orig = httpx.Client

    def spy(*a, **k):
        captured.clear(); captured.update(k)
        k.pop("proxy", None)  # don't hand a bogus proxy to the real client
        return orig(*a, **k)

    sess = {"jwt": "x", "jwt_exp": time.time() + 3600}  # unexpired → get_jwt stays offline
    httpx.Client = spy
    try:
        stt.authed_client(sess, save=lambda _s: None, proxy="http://p").close()
    finally:
        httpx.Client = orig
    assert captured.get("proxy") == "http://p", f"authed_client must thread proxy, got {captured}"

    captured.clear()
    httpx.Client = spy
    try:
        stt.authed_client(sess, save=lambda _s: None).close()
    finally:
        httpx.Client = orig
    assert captured.get("proxy") is None, "no proxy kwarg means direct (no proxy passed to httpx)"


if __name__ == "__main__":
    sys.exit(run())
