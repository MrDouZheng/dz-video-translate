"""不改变模型或输入数据的本地 smoke test。可选 --llama 验证 7B GGUF。"""

from __future__ import annotations

import argparse
import subprocess
import tempfile
from pathlib import Path

import app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--llama", action="store_true", help="启动当前配置的第一个 Hy-MT2 GGUF 并做一条翻译请求")
    parser.add_argument("--burn", action="store_true", help="用 1 秒测试画面验证 FFmpeg 字幕烧录滤镜")
    parser.add_argument("--pipeline", action="store_true", help="用固定测试字幕跑完整翻译与烧录流程")
    args = parser.parse_args()

    models = app.discover_models()
    assert models, "没有扫描到 GGUF 模型"
    assert any("Hy-MT2" in item.name for item in models), "指定目录中没有发现 Hy-MT2 模型"
    assert app.parse_translation_json('["你好", "这是测试"]', 2) == ["你好", "这是测试"]

    with tempfile.TemporaryDirectory(prefix="xiaohu-smoke-") as folder:
        folder_path = Path(folder)
        srt = folder_path / "test.srt"
        app.write_srt([{"start": 0.0, "end": 1.5, "text": "本地测试", "original": "Local test."}], srt)
        items = app.parse_srt(srt)
        assert len(items) == 1 and items[0]["text"] == "本地测试"
        ass = folder_path / "test.ass"
        app.make_ass([{"start": 0.0, "end": 1.5, "text": "本地测试", "original": "Local test."}], ass)
        assert ass.exists() and "Microsoft YaHei" in ass.read_text(encoding="utf-8")

        if args.burn:
            ffmpeg = app.executable(str(app.CONFIG.get("ffmpeg", "")), ("ffmpeg.exe", "ffmpeg"))
            assert ffmpeg, "找不到 FFmpeg"
            source = folder_path / "source.mp4"
            output = folder_path / "burned.mp4"
            subprocess.run([ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=black:s=640x360:d=1", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            vf = f"subtitles={app.ffmpeg_filter_path(srt)}:force_style='FontName=Microsoft YaHei,Bold=1,FontSize=20,PrimaryColour=&H00FFFFFF,OutlineColour=&H40000000,Outline=1,Shadow=0,MarginV=30'"
            burn_result = subprocess.run([ffmpeg, "-y", "-i", str(source), "-vf", vf, "-c:v", "libx264", "-an", str(output)], check=False, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if burn_result.returncode != 0:
                raise RuntimeError("FFmpeg burn failed:\n" + burn_result.stderr[-3000:])
            assert output.exists() and output.stat().st_size > 0
            print("ffmpeg burn ok:", output.stat().st_size, "bytes")
            bilingual_output = folder_path / "bilingual-burned.mp4"
            bilingual_vf = f"ass={app.ffmpeg_filter_path(ass)}"
            bilingual_result = subprocess.run([ffmpeg, "-y", "-i", str(source), "-vf", bilingual_vf, "-c:v", "libx264", "-an", str(bilingual_output)], check=False, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if bilingual_result.returncode != 0:
                raise RuntimeError("FFmpeg bilingual burn failed:\n" + bilingual_result.stderr[-3000:])
            assert bilingual_output.exists() and bilingual_output.stat().st_size > 0
            print("ffmpeg bilingual burn ok:", bilingual_output.stat().st_size, "bytes")

        if args.llama:
            model = next(item for item in models if "Hy-MT2-7B" in item.name)
            job = app.Job("smoke", folder_path)
            server = app.LocalLlama(model, job, folder_path / "llama.log")
            try:
                server.start()
                result = server.translate([{"text": "Hello world."}], "en")
                assert len(result) == 1 and result[0]
                print("llama translation:", result[0])
            finally:
                server.stop()

        if args.pipeline:
            ffmpeg = app.executable(str(app.CONFIG.get("ffmpeg", "")), ("ffmpeg.exe", "ffmpeg"))
            assert ffmpeg, "找不到 FFmpeg"
            source = folder_path / "pipeline-source.mp4"
            subprocess.run([ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=black:s=640x360:d=2", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            original_transcribe = app.transcribe
            app.transcribe = lambda audio_path, job, language, model_value="": ([{"start": 0.1, "end": 1.2, "text": "Hello world."}], "en")
            job = app.Job("pipeline", folder_path)
            try:
                app.run_job(job, source, "", {"model_id": next(item.model_id for item in models if "Hy-MT2-7B" in item.name), "source_language": "en", "subtitle_mode": "bilingual", "output_subtitle": "1", "output_video": "1", "whisper_model": ""})
            finally:
                app.transcribe = original_transcribe
            assert job.status == "completed", job.error
            assert len(job.outputs) == 2
            assert all((folder_path / Path(output["name"])).exists() for output in job.outputs)
            print("full pipeline ok:", [output["name"] for output in job.outputs])

    print(f"smoke ok: {len(models)} model(s)")


if __name__ == "__main__":
    main()
