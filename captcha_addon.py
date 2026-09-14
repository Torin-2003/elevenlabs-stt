#!/usr/bin/env python3
"""Resolve captcha-solver browser addons for Camoufox (Firefox).

Camoufox can load extracted Firefox addons (a dir containing manifest.json) via
its `addons=[...]` launch arg. This downloads/caches known solvers by name so a
visible hCaptcha challenge (which the invisible pass sometimes fails, esp. on
datacenter IPs) gets auto-solved in-browser.

Known: 'nopecha' — NopeCHA free tier auto-solves hCaptcha (100/day, no API key).
You can also pass a filesystem path to an already-extracted addon dir or an .xpi.
Open-source solver (hcaptcha-challenger) is a separate, async path — see docs.
"""
from __future__ import annotations

import io
import pathlib
import urllib.request
import zipfile

_CACHE = pathlib.Path.home() / ".cache" / "elevenlabs-stt" / "addons"
# AMO "latest" xpi (an .xpi is a zip). noptcha = "NopeCHA: CAPTCHA Solver".
_KNOWN = {
    "nopecha": "https://addons.mozilla.org/firefox/downloads/latest/noptcha/latest.xpi",
}


def _extract_xpi(data: bytes, dest: pathlib.Path) -> str:
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        z.extractall(dest)
    if not (dest / "manifest.json").exists():
        raise SystemExit(f"addon 解压后缺 manifest.json: {dest}")
    return str(dest)


def resolve_addon(name_or_path: str) -> str:
    """Return an extracted-addon dir path suitable for Camoufox `addons=`.

    `name_or_path` may be: a known solver name ('nopecha'); a path to an already
    extracted addon dir (has manifest.json); or a path to an .xpi file. Known
    names are downloaded from AMO and cached under ~/.cache/elevenlabs-stt/addons.
    """
    p = pathlib.Path(name_or_path).expanduser()
    if p.is_dir() and (p / "manifest.json").exists():
        return str(p)
    if p.is_file() and p.suffix == ".xpi":
        return _extract_xpi(p.read_bytes(), _CACHE / p.stem)
    url = _KNOWN.get(name_or_path.lower())
    if not url:
        raise SystemExit(
            f"未知 captcha addon: {name_or_path!r}；可用名: {list(_KNOWN)}，"
            "或传一个已解压 addon 目录/.xpi 路径")
    dest = _CACHE / name_or_path.lower()
    if (dest / "manifest.json").exists():
        return str(dest)  # cached
    data = urllib.request.urlopen(url, timeout=60).read()  # noqa: S310 (AMO https)
    return _extract_xpi(data, dest)


def resolve_addons(names: list[str]) -> list[str]:
    """Resolve a list of addon names/paths, skipping blanks."""
    return [resolve_addon(n) for n in names if str(n).strip()]


if __name__ == "__main__":  # `python3 captcha_addon.py nopecha` to fetch+inspect
    import json
    import sys
    path = resolve_addon(sys.argv[1] if len(sys.argv) > 1 else "nopecha")
    man = json.loads((pathlib.Path(path) / "manifest.json").read_text())
    print("addon dir:", path)
    print("name:", man.get("name"), "| version:", man.get("version"),
          "| mv:", man.get("manifest_version"))
