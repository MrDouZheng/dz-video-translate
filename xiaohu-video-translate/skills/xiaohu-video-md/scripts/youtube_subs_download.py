#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Download YouTube subtitles with an automatic fallback to browser cookies.

Why: YouTube often returns 403/SABR/PO Token/captcha-like blocks for subtitle/audio
requests when running yt-dlp without cookies.

This script follows the skill rule: resolve output_dir from config.json before
writing anything.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]


def _load_output_root(outdir: str | None) -> Path:
    if outdir:
        root = Path(os.path.expanduser(outdir)).resolve()
    else:
        cfg = json.load(open(SKILL_DIR / "config.json", "r", encoding="utf-8"))
        root = Path(os.path.expanduser(str(cfg.get("output_dir", "")).strip())).resolve()

    if not root.is_absolute():
        raise SystemExit("output_dir 必须是绝对路径或以 ~ 开头")

    (root / "tmp").mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)
    return root


def _run(cmd: list[str]) -> int:
    try:
        return int(subprocess.run(cmd, check=False).returncode)
    except FileNotFoundError:
        raise SystemExit("未找到 yt-dlp，请先安装（见 初始化.md）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url", help="YouTube 视频链接")
    ap.add_argument("--outdir", help="覆盖 config.json 的 output_dir（必须是绝对路径或以 ~ 开头）")
    ap.add_argument("--proxy", default="", help="代理，例如 http://127.0.0.1:7890")
    ap.add_argument("--browser", default="chrome", help="失败后从哪个浏览器读取 cookies（默认 chrome）")
    ap.add_argument(
        "--langs",
        default="zh-Hans,zh-CN,en",
        help="字幕语言优先级（对应 yt-dlp --sub-lang）",
    )
    ap.add_argument(
        "--force-cookies",
        action="store_true",
        help="跳过第一次尝试，直接使用 --cookies-from-browser",
    )
    args = ap.parse_args()

    output_root = _load_output_root(args.outdir)
    outtmpl = str(output_root / "tmp" / "%(id)s.%(ext)s")

    base = [
        "yt-dlp",
        "--skip-download",
        "--write-subs",
        "--write-auto-subs",
        "--sub-lang",
        args.langs,
        "-o",
        outtmpl,
        args.url,
    ]
    if args.proxy:
        base = base[:-1] + ["--proxy", args.proxy, args.url]

    if not args.force_cookies:
        rc = _run(base)
        if rc == 0:
            return 0

    # Retry with browser cookies.
    retry = base[:-1] + ["--cookies-from-browser", args.browser, args.url]
    return _run(retry)


if __name__ == "__main__":
    raise SystemExit(main())

