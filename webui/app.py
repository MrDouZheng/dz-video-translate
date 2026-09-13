#!/usr/bin/env python3
"""小互本地视频翻译台。

这是一个不依赖云端 API 的 Windows 本地 WebUI：
视频/URL -> FFmpeg 音频 -> faster-whisper -> llama.cpp 本地翻译 -> SRT/ASS -> 烧录视频。

上游仓库保留在 ../xiaohu-video-translate/，本文件是面向 Windows CUDA 的独立入口。
"""

from __future__ import annotations

import argparse
import warnings

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import cgi
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
DEFAULT_CONFIG: dict[str, Any] = {
    "models_dir": r"E:\path\to\llama.cpp-hub\models",
    "llama_server": r"E:\path\to\llama.cpp-hub\llama-server.exe",
    "ffmpeg": "ffmpeg",
    "yt_dlp": "yt-dlp",
    "whisper_model": "large-v3-turbo",
    "output_dir": "./data",
    "host": "127.0.0.1",
    "port": 8877,
    "whisper_device": "cuda",
    "whisper_compute_type": "float16",
    "llama_context": 8192,
    "llama_gpu_layers": 99,
    "translation_batch": 16,
}


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg.update(loaded)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"配置文件读取失败，将使用内置默认值：{exc}")
    return cfg


CONFIG = load_config()


def expand_path(value: str | Path) -> Path:
    p = Path(os.path.expandvars(os.path.expanduser(str(value))))
    return p if p.is_absolute() else (ROOT / p).resolve()


def configured_path(key: str) -> Path:
    return expand_path(str(CONFIG.get(key, "")))


def resolved_output_dir() -> Path:
    p = configured_path("output_dir")
    p.mkdir(parents=True, exist_ok=True)
    return p


def json_response(handler: BaseHTTPRequestHandler, payload: Any, status: int = 200) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def safe_name(value: str, fallback: str = "video") -> str:
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", value).strip(" .")
    return (value or fallback)[:120]


def human_size(size: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def executable(value: str, fallback_names: Iterable[str] = ()) -> str | None:
    p = expand_path(value) if value else None
    if p and p.exists():
        return str(p)
    for name in fallback_names:
        found = shutil.which(name)
        if found:
            return found
    found = shutil.which(value) if value else None
    return found


@dataclass(frozen=True)
class ModelChoice:
    model_id: str
    name: str
    path: Path
    size: int
    folder: str

    @property
    def label(self) -> str:
        return f"{self.name} · {human_size(self.size)}"


def discover_models() -> list[ModelChoice]:
    models_dir = configured_path("models_dir")
    if not models_dir.is_dir():
        return []
    candidates = []
    for path in models_dir.rglob("*.gguf"):
        name_lower = path.name.lower()
        if "mmproj" in name_lower or "projector" in name_lower:
            continue
        # 分片 GGUF 只展示第一片，llama.cpp 会按标准文件名自动续载。
        if re.search(r"-\d{5}-of-\d{5}\.gguf$", path.name, re.I):
            if not re.search(r"-00001-of-\d{5}\.gguf$", path.name, re.I):
                continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        candidates.append(
            ModelChoice(
                model_id=str(path.resolve()),
                name=path.stem,
                path=path.resolve(),
                size=size,
                folder=path.parent.name,
            )
        )
    # 翻译模型优先，7B 默认更适合 16GB 显存；仍展示所有可用 GGUF。
    candidates.sort(key=lambda x: (0 if "hy-mt" in x.name.lower() else 1, x.size, x.name.lower()))
    return candidates


def find_model(model_id: str) -> ModelChoice | None:
    for item in discover_models():
        if item.model_id == model_id:
            return item
    return None


def ffmpeg_filter_path(path: Path) -> str:
    """转换成 FFmpeg filter 可接受的 Windows 路径。"""
    value = str(path.resolve()).replace("\\", "/")
    value = value.replace("'", r"\'").replace(":", r"\:")
    # Windows 盘符中的冒号必须转义，整体加引号可避免 libavfilter 把路径误解析成选项。
    return "'" + value + "'"


def format_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def parse_timestamp(value: str) -> float:
    hms, ms = re.split(r"[,\.]", value.strip())
    h, m, s = [int(x) for x in hms.split(":")]
    return h * 3600 + m * 60 + int(s) + int(ms[:3]) / 1000


def write_srt(items: list[dict[str, Any]], path: Path) -> None:
    with path.open("w", encoding="utf-8-sig", newline="\n") as f:
        for index, item in enumerate(items, 1):
            f.write(f"{index}\n")
            f.write(f"{format_timestamp(item['start'])} --> {format_timestamp(item['end'])}\n")
            f.write(f"{item['text'].strip()}\n\n")


def parse_srt(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    blocks = re.split(r"\n\s*\n", text.strip())
    result = []
    for block in blocks:
        lines = [line.strip("\ufeff\r") for line in block.splitlines()]
        if len(lines) < 3:
            continue
        match = re.search(
            r"(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})",
            lines[1],
        )
        if not match:
            continue
        content = " ".join(line.strip() for line in lines[2:] if line.strip())
        if content:
            result.append({"start": parse_timestamp(match.group(1)), "end": parse_timestamp(match.group(2)), "text": content})
    return result


def strip_punctuation(text: str) -> str:
    # 仅去掉字幕常见标点，保留技术名词中的连字符、斜杠和百分号。
    return re.sub(r"[，。！？；：、,.!?;:'\"“”‘’（）()【】\[\]{}<>…·]", "", text).strip()


def make_ass(items: list[dict[str, Any]], path: Path, cn_size: int = 20, en_size: int = 12) -> None:
    header = """[Script Info]
ScriptType: v4.00+
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Microsoft YaHei,{cn_size},&H00FFFFFF,&H000000FF,&H64000000,&H00000000,1,0,0,0,100,100,0,0,1,1.2,0,2,20,20,16,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    for item in items:
        start = format_ass_time(item["start"])
        end = format_ass_time(item["end"])
        cn = item["text"].replace("\n", " ").strip()
        original = item.get("original", "").replace("\n", " ").strip()
        if original:
            text = f"{escape_ass(cn)}\\N{{\\fs{en_size}}}{escape_ass(original)}"
        else:
            text = escape_ass(cn)
        lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def format_ass_time(seconds: float) -> str:
    total_cs = max(0, int(round(seconds * 100)))
    h, rem = divmod(total_cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def escape_ass(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def run_command(cmd: list[str], cwd: Path | None, job: "Job", stage: str, log_path: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    job.update(stage, job.progress, "正在执行：" + Path(cmd[0]).name)
    with log_path.open("a", encoding="utf-8", errors="replace") as log:
        log.write("\n$ " + subprocess.list2cmdline(cmd) + "\n")
        process = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        output_lines: list[str] = []
        assert process.stdout is not None
        for line in process.stdout:
            line = line.rstrip()
            log.write(line + "\n")
            log.flush()
            if line:
                output_lines.append(line)
                job.append_log(line)
        returncode = process.wait()
    result = subprocess.CompletedProcess(cmd, returncode, "\n".join(output_lines), "")
    if check and returncode != 0:
        raise RuntimeError(f"{Path(cmd[0]).name} 执行失败，退出码 {returncode}")
    return result


class LocalLlama:
    def __init__(self, model: ModelChoice, job: "Job", log_path: Path):
        self.model = model
        self.job = job
        self.log_path = log_path
        self.process: subprocess.Popen[str] | None = None
        self.log_handle = None
        self.port = 18000 + (os.getpid() + int(uuid.uuid4().int % 1000)) % 20000
        self.base_url = f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        server = executable(str(CONFIG.get("llama_server", "")), ("llama-server.exe", "llama-server"))
        if not server:
            raise RuntimeError("找不到 llama-server.exe，请在 config.json 配置 llama_server")
        if not self.model.path.exists():
            raise RuntimeError(f"所选翻译模型不存在：{self.model.path}")
        cmd = [
            server,
            "--model", str(self.model.path),
            "--host", "127.0.0.1",
            "--port", str(self.port),
            "--ctx-size", str(int(CONFIG.get("llama_context", 8192))),
            "--n-gpu-layers", str(int(CONFIG.get("llama_gpu_layers", 99))),
            "--parallel", "1",
            "--temp", "0.15",
            "--top-p", "0.9",
            "--log-disable",
        ]
        self.job.update("model", 24, f"启动本地翻译模型：{self.model.label}")
        log = self.log_path.open("a", encoding="utf-8")
        self.log_handle = log
        log.write("\n$ " + subprocess.list2cmdline(cmd) + "\n")
        log.flush()
        self.process = subprocess.Popen(
            cmd,
            cwd=str(Path(server).parent),
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.time() + 240
        last_error = ""
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("llama-server 启动失败，请查看任务日志；常见原因是显存不足或模型格式不兼容")
            try:
                with urlopen(self.base_url + "/health", timeout=2) as response:
                    if response.status == 200:
                        self.job.append_log("本地翻译模型已就绪")
                        return
            except (OSError, HTTPError, URLError) as exc:
                last_error = str(exc)
            time.sleep(0.5)
        raise RuntimeError(f"等待本地翻译模型超时：{last_error or '未知错误'}")

    def translate(self, entries: list[dict[str, Any]], source_language: str) -> list[str]:
        results: list[str] = []
        batch_size = max(1, int(CONFIG.get("translation_batch", 16)))
        for start in range(0, len(entries), batch_size):
            batch = entries[start:start + batch_size]
            payload = [{"id": index, "text": item["text"]} for index, item in enumerate(batch)]
            system = (
                "你是专业的视频字幕翻译编辑。把用户提供的字幕逐条翻译为自然、简洁的简体中文。"
                "必须保留技术名词、品牌名和人名的原文拼写；不要合并或遗漏句子；不要添加解释。"
                "只输出一个 JSON 数组，数组每项是对应顺序的中文字符串，不要 Markdown 代码块。"
            )
            user = (
                f"原文语言：{source_language or '自动识别'}\n"
                "请翻译下面的 JSON 数组。数组长度必须完全相同：\n"
                + json.dumps(payload, ensure_ascii=False)
            )
            data = {
                "model": self.model.name,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "temperature": 0.15,
                "top_p": 0.9,
                "max_tokens": max(256, len(batch) * 80),
                "stream": False,
            }
            raw = self._request(data)
            translated = parse_translation_json(raw, len(batch))
            results.extend(translated)
            self.job.update("translate", min(70, 32 + int(38 * min(len(entries), start + len(batch)) / max(1, len(entries)))), f"已翻译 {len(results)}/{len(entries)} 条")
        return results

    def _request(self, payload: dict[str, Any]) -> str:
        request = Request(
            self.base_url + "/v1/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=600) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"本地翻译模型请求失败：HTTP {exc.code} {detail}") from exc
        except (OSError, URLError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"无法连接本地翻译模型：{exc}") from exc
        try:
            return str(body["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"本地翻译模型返回格式异常：{body}") from exc

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.process = None
        if self.log_handle:
            self.log_handle.close()
            self.log_handle = None


def parse_translation_json(raw: str, expected: int) -> list[str]:
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.S | re.I).strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.I | re.S).strip()
    candidates = [cleaned]
    left, right = cleaned.find("["), cleaned.rfind("]")
    if left >= 0 and right > left:
        candidates.append(cleaned[left:right + 1])
    parsed: Any = None
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            break
        except json.JSONDecodeError:
            continue
    if isinstance(parsed, dict):
        parsed = parsed.get("translations") or parsed.get("items") or parsed.get("result")
    if not isinstance(parsed, list) or len(parsed) != expected:
        raise RuntimeError(f"本地模型未返回可对齐的 JSON 翻译数组（期望 {expected} 项）")
    values = []
    for item in parsed:
        if isinstance(item, dict):
            item = item.get("translated") or item.get("translation") or item.get("text")
        if not isinstance(item, str) or not item.strip():
            raise RuntimeError("本地模型返回了空字幕，已停止以避免时间轴错位")
        values.append(strip_punctuation(item))
    return values


@dataclass
class Job:
    job_id: str
    directory: Path
    status: str = "queued"
    stage: str = "排队中"
    progress: int = 0
    message: str = ""
    language: str = ""
    logs: list[str] = field(default_factory=list)
    outputs: list[dict[str, str]] = field(default_factory=list)
    error: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def update(self, stage: str, progress: int, message: str) -> None:
        with self.lock:
            self.status = "running"
            self.stage = stage
            self.progress = max(0, min(100, progress))
            self.message = message

    def append_log(self, value: str) -> None:
        with self.lock:
            self.logs.append(value[-500:])
            if len(self.logs) > 160:
                self.logs = self.logs[-160:]

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "id": self.job_id,
                "status": self.status,
                "stage": self.stage,
                "progress": self.progress,
                "message": self.message,
                "language": self.language,
                "logs": list(self.logs),
                "outputs": list(self.outputs),
                "error": self.error,
            }


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()


def transcribe(audio_path: Path, job: Job, language: str | None, model_value: str = "") -> tuple[list[dict[str, Any]], str]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError("未安装 faster-whisper，请先运行 start_webui.ps1 安装依赖") from exc
    model_value = model_value.strip() or str(CONFIG.get("whisper_model", "large-v3-turbo"))
    model_path = expand_path(model_value)
    model_ref = str(model_path) if model_path.exists() else model_value
    requested_device = str(CONFIG.get("whisper_device", "cuda"))
    requested_compute = str(CONFIG.get("whisper_compute_type", "float16"))
    job.update("transcribe", 10, f"加载转写模型：{Path(model_ref).name}")
    try:
        model = WhisperModel(model_ref, device=requested_device, compute_type=requested_compute)
    except Exception as exc:
        if requested_device.lower() != "cpu":
            job.append_log(f"CUDA 转写不可用，降级 CPU int8：{exc}")
            model = WhisperModel(model_ref, device="cpu", compute_type="int8")
        else:
            raise RuntimeError(f"转写模型加载失败：{exc}") from exc
    segments_iter, info = model.transcribe(
        str(audio_path),
        language=language or None,
        word_timestamps=True,
        vad_filter=True,
        condition_on_previous_text=True,
    )
    detected = getattr(info, "language", "unknown") or "unknown"
    items = []
    for segment in segments_iter:
        text = (getattr(segment, "text", "") or "").strip()
        start = float(getattr(segment, "start", 0.0))
        end = float(getattr(segment, "end", start))
        if not text or end <= start:
            continue
        items.append({"start": start, "end": end, "text": text})
        job.update("transcribe", min(31, 12 + len(items) // 3), f"已转写 {len(items)} 条，检测语言：{detected}")
    if not items:
        raise RuntimeError("未识别到有效语音内容")
    return items, detected


def download_url(url: str, target_dir: Path, job: Job) -> Path:
    tool = executable(str(CONFIG.get("yt_dlp", "yt-dlp")), ("yt-dlp.exe", "yt-dlp"))
    if not tool:
        raise RuntimeError("找不到 yt-dlp，URL 输入需要先安装 yt-dlp")
    template = target_dir / "download.%(ext)s"
    cmd = [
        tool,
        "--no-playlist",
        "-f", "bestvideo[vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "-o", str(template),
        url,
    ]
    run_command(cmd, None, job, "download", target_dir / "job.log")
    files = [p for p in target_dir.glob("download.*") if p.suffix.lower() not in {".part", ".ytdl"}]
    if not files:
        raise RuntimeError("URL 下载完成但未找到视频文件")
    return max(files, key=lambda p: p.stat().st_size)


def run_job(job: Job, input_path: Path | None, url: str, options: dict[str, str]) -> None:
    model_server: LocalLlama | None = None
    log_path = job.directory / "job.log"
    try:
        job.update("prepare", 3, "准备输入文件")
        video_path = input_path
        if not video_path and url.strip():
            video_path = download_url(url.strip(), job.directory, job)
        if not video_path or not video_path.exists():
            raise RuntimeError("请上传本地视频文件，或填写视频 URL")
        job.append_log(f"输入视频：{video_path.name}")
        ffmpeg = executable(str(CONFIG.get("ffmpeg", "ffmpeg")), ("ffmpeg.exe", "ffmpeg"))
        if not ffmpeg:
            raise RuntimeError("找不到 FFmpeg，请在 config.json 配置 ffmpeg")

        audio_path = job.directory / "audio.wav"
        run_command(
            [ffmpeg, "-y", "-i", str(video_path), "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", str(audio_path)],
            None, job, "audio", log_path,
        )
        source_choice = options.get("source_language", "auto")
        language_arg = None if source_choice in {"auto", ""} else source_choice
        original, detected = transcribe(audio_path, job, language_arg, options.get("whisper_model", ""))
        job.language = detected
        write_srt(original, job.directory / "原文.srt")

        bilingual = options.get("subtitle_mode", "zh") == "bilingual"
        translated_texts: list[str]
        if detected.lower().startswith("zh"):
            translated_texts = [strip_punctuation(item["text"]) for item in original]
            job.update("translate", 70, "检测到中文，跳过重复翻译")
        else:
            model_id = options.get("model_id", "")
            model = find_model(model_id)
            if not model:
                raise RuntimeError("未找到所选本地翻译模型，请刷新模型列表后重试")
            model_server = LocalLlama(model, job, log_path)
            model_server.start()
            translated_texts = model_server.translate(original, detected)

        final_items = []
        for item, translated in zip(original, translated_texts):
            final_items.append({"start": item["start"], "end": item["end"], "text": translated, "original": item["text"]})
        subtitle_path = job.directory / ("中文字幕-中英双语.srt" if bilingual else "中文字幕.srt")
        srt_items = []
        for item in final_items:
            text = item["text"]
            if bilingual:
                text = text + "\n" + item["original"]
            srt_items.append({"start": item["start"], "end": item["end"], "text": text})
        write_srt(srt_items, subtitle_path)
        job.update("subtitle", 76, f"已生成字幕文件：{subtitle_path.name}")

        output_subtitle = options.get("output_subtitle", "1") == "1"
        output_video = options.get("output_video", "1") == "1"
        if output_subtitle:
            job.outputs.append({"name": subtitle_path.name, "url": f"/files/{quote(job.job_id)}/{quote(subtitle_path.name)}"})

        if output_video:
            output_video_path = job.directory / f"{safe_name(video_path.stem)}-中文字幕视频.mp4"
            if bilingual:
                ass_path = job.directory / "中文字幕-中英双语.ass"
                make_ass(final_items, ass_path)
                vf = f"ass={ffmpeg_filter_path(ass_path)}"
            else:
                subs = ffmpeg_filter_path(subtitle_path)
                force_style = "FontName=Microsoft YaHei,Bold=1,FontSize=20,PrimaryColour=&H00FFFFFF,OutlineColour=&H40000000,Outline=1,Shadow=0,MarginV=30"
                vf = f"subtitles={subs}:force_style='{force_style}'"
            run_command(
                [ffmpeg, "-y", "-i", str(video_path), "-map", "0:v:0", "-map", "0:a:0?", "-vf", vf,
                 "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-c:a", "aac", "-b:a", "128k",
                 "-movflags", "+faststart", str(output_video_path)],
                None, job, "burn", log_path,
            )
            job.outputs.append({"name": output_video_path.name, "url": f"/files/{quote(job.job_id)}/{quote(output_video_path.name)}"})
        with job.lock:
            job.status = "completed"
            job.stage = "完成"
            job.progress = 100
            job.message = "本地翻译与输出已完成"
    except Exception as exc:
        with job.lock:
            job.status = "failed"
            job.stage = "失败"
            job.error = str(exc)
            job.message = "任务未完成，请查看下方日志"
        job.append_log("错误：" + str(exc))
    finally:
        if model_server:
            model_server.stop()
        try:
            audio = job.directory / "audio.wav"
            if audio.exists():
                audio.unlink()
        except OSError:
            pass


HTML_PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>小互 · 本地视频翻译台</title>
  <style>
    :root { color-scheme: dark; --bg:#121418; --panel:#1b1e24; --panel2:#242932; --line:#343a46; --text:#f5f7fa; --muted:#aab1bd; --accent:#ffb454; --accent2:#e47642; --ok:#6bd39b; --bad:#ff7d7d; }
    * { box-sizing:border-box; } body { margin:0; background:radial-gradient(circle at 15% -10%,#3b2c26 0,#121418 42%); color:var(--text); font:15px/1.55 "Microsoft YaHei", "Noto Sans CJK SC", system-ui, sans-serif; }
    .wrap { max-width:1100px; margin:0 auto; padding:36px 20px 60px; } header { display:flex; align-items:flex-start; justify-content:space-between; gap:20px; margin-bottom:24px; }
    h1 { margin:0 0 8px; font-size:30px; letter-spacing:.02em; } .sub { color:var(--muted); } .badge { border:1px solid #775938; color:var(--accent); padding:6px 10px; border-radius:999px; font-size:12px; white-space:nowrap; }
    .grid { display:grid; grid-template-columns:1.05fr .95fr; gap:18px; } .card { background:rgba(27,30,36,.92); border:1px solid var(--line); border-radius:16px; padding:20px; box-shadow:0 16px 50px rgba(0,0,0,.18); }
    .card h2 { margin:0 0 16px; font-size:17px; } label { display:block; color:var(--muted); font-size:13px; margin:14px 0 7px; } input[type=text], input[type=url], select { width:100%; border:1px solid var(--line); background:#101217; color:var(--text); padding:11px 12px; border-radius:9px; outline:none; } input:focus, select:focus { border-color:var(--accent2); }
    input[type=file] { width:100%; padding:13px; border:1px dashed #5a6270; border-radius:10px; background:#101217; color:var(--muted); } .hint { color:var(--muted); font-size:12px; margin-top:6px; }
    .row { display:grid; grid-template-columns:1fr 1fr; gap:12px; } .checks { display:flex; flex-wrap:wrap; gap:12px; margin-top:12px; } .check { display:flex; align-items:center; gap:7px; color:var(--text); font-size:14px; } .check input { accent-color:var(--accent2); }
    button { border:0; border-radius:10px; background:linear-gradient(135deg,var(--accent),var(--accent2)); color:#23170e; font-weight:700; padding:12px 18px; cursor:pointer; } button.secondary { background:#2b313b; color:var(--text); border:1px solid var(--line); } button:disabled { cursor:not-allowed; opacity:.55; }
    .actions { display:flex; align-items:center; gap:10px; margin-top:20px; } .status { margin-top:20px; } .progress { height:9px; background:#0e1014; border-radius:99px; overflow:hidden; } .bar { height:100%; width:0; background:linear-gradient(90deg,var(--accent2),var(--accent)); transition:width .25s; }
    .statusline { display:flex; justify-content:space-between; gap:16px; margin:9px 0; } .stage { color:var(--accent); } .percent { color:var(--muted); } pre { white-space:pre-wrap; max-height:260px; overflow:auto; background:#0e1014; border:1px solid var(--line); padding:12px; border-radius:9px; font:12px/1.45 Consolas, monospace; color:#cbd2dc; } .outputs { display:flex; flex-wrap:wrap; gap:9px; margin-top:12px; } .outputs a { color:#1b1510; background:var(--ok); padding:8px 11px; border-radius:8px; text-decoration:none; font-weight:700; font-size:13px; }
    .full { grid-column:1 / -1; } .note { border-left:3px solid var(--accent2); padding:8px 12px; color:var(--muted); background:#211c1b; border-radius:0 8px 8px 0; font-size:13px; } .small { font-size:12px; color:var(--muted); }
    @media (max-width:800px) { .grid { grid-template-columns:1fr; } .full { grid-column:auto; } header { display:block; } .badge { display:inline-block; margin-top:12px; } .row { grid-template-columns:1fr; } }
  </style>
</head>
<body>
<main class="wrap">
  <header><div><h1>小互 · 本地视频翻译台</h1><div class="sub">本地转写、翻译、字幕导出与视频烧录，一条链路完成</div></div><div class="badge">离线优先 · Windows CUDA</div></header>
  <div class="grid">
    <section class="card">
      <h2>① 选择视频</h2>
      <label for="video">本地视频文件</label><input id="video" type="file" accept="video/*,audio/*">
      <div class="hint">如果同时填写 URL，将优先使用本地文件。</div>
      <label for="url">视频 URL（可选）</label><input id="url" type="url" placeholder="https://…">
      <div class="note" style="margin-top:15px">所有处理都在本机执行。URL 下载需要本机已安装 yt-dlp，受站点登录、网络与版权条件影响。</div>
    </section>
    <section class="card">
      <h2>② 本地模型与输出</h2>
      <label for="model">翻译模型（来自指定 models 目录）</label>
      <select id="model"><option>正在扫描模型…</option></select>
      <div class="actions" style="margin-top:8px"><button id="refresh" class="secondary" type="button">刷新模型列表</button><span id="modelInfo" class="small"></span></div>
      <label for="whisperModel">转写模型目录</label><input id="whisperModel" type="text">
      <div class="hint">默认使用本机 faster-whisper large-v3；首次运行不会调用翻译云 API。</div>
      <label for="outputDir">输出目录</label><input id="outputDir" type="text">
    </section>
    <section class="card">
      <h2>③ 翻译设置</h2>
      <div class="row"><div><label for="source">原语种</label><select id="source"><option value="auto">自动识别</option><option value="en">英语</option><option value="ja">日语</option><option value="ko">韩语</option><option value="fr">法语</option><option value="de">德语</option><option value="es">西班牙语</option><option value="zh">中文</option></select></div><div><label for="mode">字幕模式</label><select id="mode"><option value="zh">中文字幕</option><option value="bilingual">中英双语字幕</option></select></div></div>
      <div class="checks"><label class="check"><input id="outputSubtitle" type="checkbox" checked> 输出字幕文件（SRT）</label><label class="check"><input id="outputVideo" type="checkbox" checked> 输出烧录字幕视频（MP4）</label></div>
      <div class="hint">双语视频使用 ASS 烧录，实现中文大字、原文小字；单语视频使用微软雅黑与黑边样式。</div>
      <div class="actions"><button id="start" type="button">开始本地翻译</button><span class="small">建议先用 1–2 分钟短片验收模型和字体</span></div>
    </section>
    <section class="card">
      <h2>任务状态</h2>
      <div class="statusline"><span id="stage" class="stage">尚未开始</span><span id="percent" class="percent">0%</span></div><div class="progress"><div id="bar" class="bar"></div></div><div id="message" class="hint" style="margin-top:9px">请选择视频后开始</div>
      <div id="outputs" class="outputs"></div>
      <pre id="logs">等待任务日志…</pre>
    </section>
    <section class="card full"><h2>本地化边界</h2><div class="small">字幕时间轴来自 Whisper 转写；翻译由所选 GGUF 模型在本机 llama.cpp 中完成；FFmpeg 仅用于提取音频和烧录。渲染完成、接口成功或生成文件不等于人工审核或公开发布，发布前请抽查字幕准确性、专有名词、字体和时间轴。</div></section>
  </div>
</main>
<script>
const $ = id => document.getElementById(id); let currentJob = null; let pollTimer = null;
function setText(id, value) { $(id).textContent = value ?? ''; }
async function loadConfig() { const r = await fetch('/api/config'); const d = await r.json(); $('whisperModel').value = d.whisper_model; $('outputDir').value = d.output_dir; }
async function loadModels() { const s=$('model'); s.innerHTML='<option>正在扫描模型…</option>'; const r=await fetch('/api/models'); const d=await r.json(); s.innerHTML=''; if(!d.models.length){s.innerHTML='<option value="">未找到 GGUF 模型</option>'; $('modelInfo').textContent='请检查模型目录'; return;} for(const m of d.models){const o=document.createElement('option');o.value=m.id;o.textContent=m.label;s.appendChild(o);} $('modelInfo').textContent=`共 ${d.models.length} 个可选模型 · 默认优先 Hy-MT2 7B`; }
function renderJob(d) { $('bar').style.width=d.progress+'%'; setText('percent',d.progress+'%'); setText('stage',d.stage); setText('message',d.message || d.error); $('logs').textContent=(d.logs||[]).join('\n') || '等待任务日志…'; $('logs').scrollTop=$('logs').scrollHeight; $('outputs').innerHTML=''; for(const out of (d.outputs||[])){const a=document.createElement('a');a.href=out.url;a.download=out.name;a.textContent='下载 '+out.name;$('outputs').appendChild(a);} if(d.status==='completed'||d.status==='failed'){ $('start').disabled=false; if(d.status==='failed') $('stage').style.color='var(--bad)'; if(pollTimer){clearInterval(pollTimer);pollTimer=null;} } }
async function poll(){ if(!currentJob)return; const r=await fetch('/api/status?id='+encodeURIComponent(currentJob)); renderJob(await r.json()); }
async function startJob(){ if(!$('video').files.length && !$('url').value.trim()){alert('请先选择本地视频或填写视频 URL');return;} if(!$('outputSubtitle').checked&&!$('outputVideo').checked){alert('至少选择一种输出');return;} const fd=new FormData(); if($('video').files.length)fd.append('video',$('video').files[0]); fd.append('url',$('url').value); fd.append('model_id',$('model').value); fd.append('source_language',$('source').value); fd.append('subtitle_mode',$('mode').value); fd.append('output_subtitle',$('outputSubtitle').checked?'1':'0'); fd.append('output_video',$('outputVideo').checked?'1':'0'); fd.append('whisper_model',$('whisperModel').value); fd.append('output_dir',$('outputDir').value); $('start').disabled=true; setText('message','正在提交任务…'); const r=await fetch('/api/translate',{method:'POST',body:fd}); const d=await r.json(); if(!r.ok){alert(d.error||'提交失败');$('start').disabled=false;return;} currentJob=d.id; if(pollTimer)clearInterval(pollTimer); await poll(); pollTimer=setInterval(poll,900); }
$('refresh').onclick=loadModels; $('start').onclick=startJob; loadConfig(); loadModels();
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "XiaohuLocalWebUI/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            data = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if parsed.path == "/api/config":
            output_dir = resolved_output_dir()
            json_response(self, {"whisper_model": str(CONFIG.get("whisper_model", "")), "output_dir": str(output_dir), "models_dir": str(configured_path("models_dir"))})
            return
        if parsed.path == "/api/models":
            json_response(self, {"models": [{"id": m.model_id, "name": m.name, "label": m.label, "folder": m.folder, "size": m.size} for m in discover_models()]})
            return
        if parsed.path == "/api/status":
            from urllib.parse import parse_qs
            job_id = parse_qs(parsed.query).get("id", [""])[0]
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            if not job:
                json_response(self, {"error": "任务不存在"}, 404)
            else:
                json_response(self, job.snapshot())
            return
        if parsed.path.startswith("/files/"):
            self.serve_file(parsed.path)
            return
        self.send_error(404)

    def serve_file(self, path: str) -> None:
        parts = path.split("/")
        if len(parts) < 4:
            self.send_error(404)
            return
        job_id = parts[2]
        filename = os.path.basename(parts[3])
        with JOBS_LOCK:
            job = JOBS.get(job_id)
        if not job:
            self.send_error(404)
            return
        candidate = (job.directory / filename).resolve()
        try:
            candidate.relative_to(job.directory.resolve())
        except ValueError:
            self.send_error(403)
            return
        if not candidate.is_file():
            self.send_error(404)
            return
        data = candidate.read_bytes()
        content_type = "video/mp4" if candidate.suffix.lower() == ".mp4" else "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(candidate.name)}")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/translate":
            json_response(self, {"error": "接口不存在"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                raise RuntimeError("请求内容为空")
            form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": self.headers.get("Content-Type", ""), "CONTENT_LENGTH": str(length)}, keep_blank_values=True)
            output_dir_value = str(form.getfirst("output_dir") or CONFIG.get("output_dir", "./data"))
            target_root = expand_path(output_dir_value)
            target_root.mkdir(parents=True, exist_ok=True)
            job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
            job_dir = target_root / "jobs" / job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            input_path: Path | None = None
            if "video" in form:
                field = form["video"]
                if getattr(field, "filename", None):
                    filename = safe_name(Path(field.filename).name, "input.mp4")
                    suffix = Path(filename).suffix or ".mp4"
                    input_path = job_dir / ("input" + suffix)
                    with input_path.open("wb") as out:
                        while True:
                            chunk = field.file.read(1024 * 1024)
                            if not chunk:
                                break
                            out.write(chunk)
            options = {key: str(form.getfirst(key) or "") for key in ("model_id", "source_language", "subtitle_mode", "output_subtitle", "output_video", "whisper_model")}
            url = str(form.getfirst("url") or "")
            job = Job(job_id, job_dir)
            with JOBS_LOCK:
                JOBS[job_id] = job
            thread = threading.Thread(target=run_job, args=(job, input_path, url, options), name=f"video-job-{job_id}", daemon=True)
            thread.start()
            json_response(self, {"id": job_id})
        except Exception as exc:
            json_response(self, {"error": str(exc)}, 400)


def main() -> None:
    global CONFIG
    parser = argparse.ArgumentParser(description="小互本地视频翻译台")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--config", default=str(CONFIG_PATH))
    args = parser.parse_args()
    CONFIG = load_config(Path(args.config).resolve())
    host = args.host or str(CONFIG.get("host", "127.0.0.1"))
    port = args.port or int(CONFIG.get("port", 8877))
    output = resolved_output_dir()
    server = ThreadingHTTPServer((host, port), Handler)
    print("小互本地视频翻译台已启动")
    print(f"浏览器打开：http://{host}:{port}")
    print(f"模型目录：{configured_path('models_dir')}")
    print(f"输出目录：{output}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止 WebUI")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
