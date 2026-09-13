#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YouTube download helper for this skill.

Goal: keep the "happy path" fast, but if yt-dlp fails (often 403/SABR/PO token),
retry automatically with browser cookies (e.g. Chrome).

This script follows the skill rule: read config.json first to resolve output_dir.
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
        cfg_path = SKILL_DIR / "config.json"
        cfg = json.load(open(cfg_path, "r", encoding="utf-8"))
        raw = str(cfg.get("output_dir", "")).strip()
        root = Path(os.path.expanduser(raw)).resolve()

    if not root.is_absolute():
        raise SystemExit("output_dir 必须是绝对路径或以 ~ 开头")

    (root / "tmp").mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)
    return root


def _run(cmd: list[str]) -> int:
    # Let yt-dlp stream its own progress to the terminal.
    try:
        p = subprocess.run(cmd, check=False)
        return int(p.returncode)
    except FileNotFoundError:
        raise SystemExit("未找到 yt-dlp，请先安装（见 初始化.md）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url", help="YouTube 视频链接")
    ap.add_argument("--outdir", help="覆盖 config.json 的 output_dir（必须是绝对路径或以 ~ 开头）")
    ap.add_argument(
        "--browser",
        default="chrome",
        help="失败后用哪个浏览器的 cookies 进行重试（默认：chrome；也可 firefox/safari/brave/edge 等）",
    )
    ap.add_argument("--proxy", default="", help="代理，例如 http://127.0.0.1:7890")
    ap.add_argument(
        "--format",
        # 优先 H.264 (avc1) 编码。YouTube mp4 容器可装 AV1，而 X/Twitter 等社交平台不支持 AV1。
        # 2026-04-24 踩坑：默认拿到 format 399(AV1)导致 X 上传失败。
        default='bestvideo[vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        help="yt-dlp -f 参数（默认优先 H.264 avc1，兼容 X/Twitter 上传）",
    )
    ap.add_argument(
        "--output",
        # Avoid clobbering an older low-res file that used the plain title name.
        default="%(title).40s-best.%(ext)s",
        help="输出文件名模板（会写入 <输出根>/data/ 下）",
    )
    ap.add_argument(
        "--force-cookies",
        action="store_true",
        help="跳过第一次尝试，直接使用 --cookies-from-browser",
    )
    args = ap.parse_args()

    output_root = _load_output_root(args.outdir)
    outtmpl = str(output_root / "data" / args.output)

    base = [
        "yt-dlp",
        "--restrict-filenames",
        "--retries",
        "10",
        "--fragment-retries",
        "10",
        "--concurrent-fragments",
        "1",
        "-f",
        args.format,
        "-o",
        outtmpl,
    ]
    if args.proxy:
        base += ["--proxy", args.proxy]

    # Attempt 1: no cookies (faster; works on older/less protected videos).
    if not args.force_cookies:
        rc = _run(base + [args.url])
        if rc == 0:
            return 0

    # Attempt 2: fall back to browser cookies (more reliable for YouTube SABR/403).
    fallback = base + ["--cookies-from-browser", args.browser, args.url]
    rc2 = _run(fallback)
    return rc2


if __name__ == "__main__":
    raise SystemExit(main())
