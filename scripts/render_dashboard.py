#!/usr/bin/env python3
"""Render the 台股觀測站 dashboard by injecting premarket/postmarket JSON state into the HTML template."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BUNDLE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from md_to_html import markdown_to_html  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", type=Path, default=BUNDLE_DIR / "scripts" / "dashboard_template.html")
    parser.add_argument("--premarket", type=Path, default=BUNDLE_DIR / "state" / "premarket.json")
    parser.add_argument("--postmarket", type=Path, default=BUNDLE_DIR / "state" / "postmarket.json")
    parser.add_argument("--report", type=Path, default=BUNDLE_DIR / "state" / "full_report.json")
    parser.add_argument("--output", type=Path, default=BUNDLE_DIR / "state" / "dashboard.html")
    args = parser.parse_args()

    template = args.template.read_text(encoding="utf-8")
    premarket = json.loads(args.premarket.read_text(encoding="utf-8"))
    postmarket = json.loads(args.postmarket.read_text(encoding="utf-8"))
    report = json.loads(args.report.read_text(encoding="utf-8")) if args.report.exists() else {
        "generated_at": None,
        "as_of": None,
        "title": "深度研究報告",
        "markdown": "",
    }
    def dumps_for_script(data: dict) -> str:
        # Escape "</" so embedded text can never prematurely close the <script> tag.
        return json.dumps(data, ensure_ascii=False).replace("</", "<\\/")

    # If the report already carries pre-rendered HTML (e.g. extracted verbatim
    # from the live published page by a routine that doesn't own this week's
    # report), use it as-is instead of re-converting markdown.
    if report.get("html"):
        report_html = report["html"]
        report_meta = {k: v for k, v in report.items() if k not in ("markdown", "html")}
    else:
        report_html = markdown_to_html(report.get("markdown") or "")
        report_meta = {k: v for k, v in report.items() if k != "markdown"}

    html = template.replace(
        "__PREMARKET_JSON__", dumps_for_script(premarket)
    ).replace(
        "__POSTMARKET_JSON__", dumps_for_script(postmarket)
    ).replace(
        "__REPORT_META_JSON__", dumps_for_script(report_meta)
    ).replace(
        "__REPORT_HTML__", report_html
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html, encoding="utf-8")
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
