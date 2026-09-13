#!/usr/bin/env python3
"""
双语 SRT → 双语 ASS 转换器。

输入：一个「双语 SRT」——每条字幕含两行内容，中文在上、英文（或其他原文）在下，
时间戳与原文一句对一句对齐。例如：
    1
    00:00:19,239 --> 00:00:21,239
    大家好吗
    Hello everyone, how are you?

输出：双语 ASS——同一条字幕内中文走默认大字号、英文用 inline 降字号，
形成「中文主导 / 英文辅助」的反差对比。烧录时用 `ass=文件.ass` 滤镜。

为什么不用 SRT + subtitles 滤镜：force_style 的 FontSize 对整条字幕统一，
没法让一条里中英不同字号；而 SRT 文本里的 inline `{\\fsN}` 会被 ffmpeg 的
srt 解码器剥离（2026-05-31 实测三档字号 md5 全同）。只有真正的 ASS 文件
喂给 libass 才能在一条内 `\\N` 换行 + inline 切字号。

字号默认按分辨率给（中文︰英文 ≈ 1.7，实测干净的反差比例）：
    360p  : 中文 22 / 英文 13
    720p  : 中文 22 / 英文 13
    1080p : 中文 20 / 英文 12
    （ASS FontSize 是相对 PlayResY=288 的相对值，libass 会再按视频分辨率缩放，
      所以不同分辨率的"相对字号"差别不大；真正决定观感的是视频本身分辨率）
用户在对话里明确给了字号（如"字号 24"）则覆盖默认，按用户中文字号 + 比例算英文。
"""

import argparse
import re
import sys
from pathlib import Path


def parse_srt(text):
    """解析 SRT，返回 [(start, end, [行1, 行2, ...]), ...]"""
    blocks = re.split(r"\n\s*\n", text.strip())
    items = []
    for b in blocks:
        lines = [l for l in b.split("\n") if l.strip() != ""]
        if len(lines) < 3:
            continue
        # lines[0]=序号, lines[1]=时间, lines[2:]=内容
        m = re.search(r"(\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,\.]\d{3})", lines[1])
        if not m:
            continue
        items.append((m.group(1), m.group(2), lines[2:]))
    return items


def srt_time_to_ass(t):
    """00:00:19,239 -> 0:00:19.24"""
    t = t.replace(".", ",")
    hms, ms = t.split(",")
    h, m, s = hms.split(":")
    cs = int(round(int(ms) / 10.0))
    if cs >= 100:
        cs = 99
    return f"{int(h)}:{m}:{s}.{cs:02d}"


ASS_HEADER = """[Script Info]
ScriptType: v4.00+
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{cn_size},&H00FFFFFF,&H000000FF,&H64000000,&H00000000,1,0,0,0,100,100,0,0,1,1.2,0,2,20,20,{marginv},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def build_ass(items, cn_size, en_size, font="PingFang SC", marginv=16):
    out = [ASS_HEADER.format(font=font, cn_size=cn_size, marginv=marginv)]
    for start, end, lines in items:
        if len(lines) >= 2:
            cn = lines[0].strip()
            en = " ".join(x.strip() for x in lines[1:])
            text = f"{cn}\\N{{\\fs{en_size}}}{en}"
        else:
            # 只有一行（纯中文条）：直接用默认字号
            text = lines[0].strip()
        out.append(
            f"Dialogue: 0,{srt_time_to_ass(start)},{srt_time_to_ass(end)},Default,,0,0,0,,{text}"
        )
    return "\n".join(out) + "\n"


# 分辨率默认字号表（中文, 英文）
SIZE_TABLE = {360: (22, 13), 720: (22, 13), 1080: (20, 12), 2160: (20, 12)}


def pick_sizes(height, cn_override=None, ratio=1.7):
    if cn_override:
        cn = cn_override
        en = max(8, round(cn / ratio))
        return cn, en
    # 选最接近的档
    key = min(SIZE_TABLE, key=lambda k: abs(k - (height or 720)))
    return SIZE_TABLE[key]


# 按文字脚本自动选字体（macOS）：默认苹方只覆盖中文 + 拉丁，
# 韩语 / 阿拉伯语 / 日语假名各需对应系统字体，否则烧出来是方块。
def _detect_script(text):
    """返回 hangul / arabic / kana / cjk / latin 之一（按优先级）"""
    has = {"hangul": False, "arabic": False, "kana": False, "cjk": False}
    for ch in text:
        o = ord(ch)
        if 0xAC00 <= o <= 0xD7A3:
            has["hangul"] = True
        elif 0x0600 <= o <= 0x06FF or 0x0750 <= o <= 0x077F:
            has["arabic"] = True
        elif 0x3040 <= o <= 0x30FF:
            has["kana"] = True
        elif 0x4E00 <= o <= 0x9FFF:
            has["cjk"] = True
    for k in ("hangul", "arabic", "kana", "cjk"):
        if has[k]:
            return k
    return "latin"


# 脚本 → macOS 字体（这些字体都自带拉丁字形，英文行同字体即可）
SCRIPT_FONT = {
    "hangul": "Apple SD Gothic Neo",
    "arabic": "Geeza Pro",
    "kana": "Hiragino Sans",
}


def pick_font_for_items(items, default="PingFang SC"):
    """扫描全部字幕文本，按主导文字脚本自动选字体；中文 / 拉丁回落默认苹方。"""
    text = "".join("".join(lines) for _, _, lines in items)
    return SCRIPT_FONT.get(_detect_script(text), default)


def main():
    ap = argparse.ArgumentParser(description="双语 SRT → 双语 ASS（中文大 / 英文小）")
    ap.add_argument("srt", help="双语 SRT 路径（中文在上、英文在下）")
    ap.add_argument("--output", "-o", help="输出 ASS 路径（默认同名 .ass）")
    ap.add_argument("--cn-size", type=int, default=None, help="中文字号（用户指定则覆盖默认表）")
    ap.add_argument("--en-size", type=int, default=None, help="英文字号（默认按中文 / 1.7 算）")
    ap.add_argument("--height", type=int, default=None, help="视频高度像素，用于选默认字号档")
    ap.add_argument("--font", default=None, help="字体名（默认按文字脚本自动选：中文苹方 / 韩文 Apple SD Gothic / 阿拉伯 Geeza Pro / 日文 Hiragino）")
    ap.add_argument("--marginv", type=int, default=16, help="底部边距（默认 16）")
    args = ap.parse_args()

    srt_path = Path(args.srt)
    if not srt_path.exists():
        print(f"错误：文件不存在 {srt_path}", file=sys.stderr)
        sys.exit(1)

    items = parse_srt(srt_path.read_text(encoding="utf-8"))
    if not items:
        print("错误：未解析到任何字幕条", file=sys.stderr)
        sys.exit(1)

    cn_size, en_size = pick_sizes(args.height, args.cn_size)
    if args.en_size:
        en_size = args.en_size

    font = args.font or pick_font_for_items(items)

    ass = build_ass(items, cn_size, en_size, font=font, marginv=args.marginv)
    out_path = args.output or str(srt_path.with_suffix(".ass"))
    Path(out_path).write_text(ass, encoding="utf-8")
    print(f"完成！{len(items)} 条 → {out_path}（中文 {cn_size} / 英文 {en_size} / 字体 {font}）", file=sys.stderr)


if __name__ == "__main__":
    main()
