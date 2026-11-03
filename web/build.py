#!/usr/bin/env python3
"""Build the self-contained index.html for the Pyodide web app.

The generated file embeds kulku.py and app.py as base64 so it works when
opened directly from disk (file://), not only through a web server.
"""

from __future__ import annotations

import base64
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def find_kulku() -> Path:
    for candidate in (
        ROOT / "kulku.py",
        ROOT.parent / "kulku.py",
        ROOT.parent / "scripts" / "kulku.py",
    ):
        if candidate.is_file():
            return candidate
    raise SystemExit("Could not find kulku.py next to web/, in the repo root, or in scripts/")


def data_uri(path: Path, mime: str) -> str:
    if not path.is_file():
        raise SystemExit(f"Missing asset: {path}")
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def main() -> None:
    template = (ROOT / "template.html").read_text(encoding="utf-8")
    kulku_source = find_kulku().read_bytes()
    app_source = (ROOT / "app.py").read_bytes()
    assets = ROOT / "assets"
    output = (
        template.replace("__KULKU_B64__", base64.b64encode(kulku_source).decode("ascii"))
        .replace("__APP_B64__", base64.b64encode(app_source).decode("ascii"))
        .replace("__HERO_BG__", data_uri(assets / "bg.png", "image/png"))
    )
    target = ROOT / "index.html"
    target.write_text(output, encoding="utf-8")
    print(f"Wrote {target} ({target.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
