#!/usr/bin/env python3
"""Render the 台股觀測站 dashboard by injecting premarket/postmarket JSON state into the HTML template."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

BUNDLE_DIR = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", type=Path, default=BUNDLE_DIR / "scripts" / "dashboard_template.html")
    parser.add_argument("--premarket", type=Path, default=BUNDLE_DIR / "state" / "premarket.json")
    parser.add_argument("--postmarket", type=Path, default=BUNDLE_DIR / "state" / "postmarket.json")
    parser.add_argument("--output", type=Path, default=BUNDLE_DIR / "state" / "dashboard.html")
    args = parser.parse_args()

    template = args.template.read_text(encoding="utf-8")
    premarket = json.loads(args.premarket.read_text(encoding="utf-8"))
    postmarket = json.loads(args.postmarket.read_text(encoding="utf-8"))

    def dumps_for_script(data: dict) -> str:
        # Escape "</" so embedded text can never prematurely close the <script> tag.
        return json.dumps(data, ensure_ascii=False).replace("</", "<\\/")

    html = template.replace(
        "__PREMARKET_JSON__", dumps_for_script(premarket)
    ).replace(
        "__POSTMARKET_JSON__", dumps_for_script(postmarket)
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html, encoding="utf-8")
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
