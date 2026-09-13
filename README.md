# 小互本地视频翻译台

这是基于 [`xiaohu-video-translate`](./xiaohu-video-translate/) 整理的独立 Windows 本地 WebUI。它将视频转写、中文翻译、字幕导出和字幕烧录串成一条本地流水线。

## 功能

- 上传本地视频，或使用 `yt-dlp` 下载 URL
- 使用 `faster-whisper` 生成带时间轴的原文字幕
- 从本地 GGUF 模型目录选择翻译模型并通过 `llama.cpp` 推理
- 输出中文字幕 SRT 或中英双语 SRT
- 用 FFmpeg 输出带烧录字幕的 MP4；双语字幕使用 ASS 实现字号层级
- 全中文界面，任务日志和产物保存在本地

## Windows 启动

1. 将 `webui/config.example.json` 复制为 `webui/config.json`。
2. 修改 `models_dir`、`llama_server`、`ffmpeg`、`whisper_model` 为本机路径。
3. 双击 `webui/start_webui.bat`。
4. 浏览器打开 <http://127.0.0.1:8877>。

模型目录按文件夹递归扫描 `.gguf`，默认优先 Hy-MT2 翻译模型。模型、FFmpeg 和转写模型均由使用者自行准备，本仓库不包含大模型文件。

## 项目来源

上游技能项目位于 [`xiaohu-video-translate/`](./xiaohu-video-translate/)，包含视频下载、转写、字幕润色和 ASS 字幕工具；`webui/` 是面向 Windows CUDA 的独立入口。

## 许可证

上游项目保留 MIT 许可证，详见 [`xiaohu-video-translate/LICENSE`](./xiaohu-video-translate/LICENSE)。
