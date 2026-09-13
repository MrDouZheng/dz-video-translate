# 小互本地视频翻译台

这是基于 `xiaohu-video-translate` 上游技能整理的独立 Windows WebUI。界面、任务状态和输出均为中文；翻译和转写在本机完成，不调用在线翻译 API。

## 已接入的本地链路

1. 上传本地视频，或填写 URL 由 `yt-dlp` 下载。
2. FFmpeg 提取 16 kHz 单声道音频。
3. 使用 faster-whisper 生成带时间轴的原文字幕。
4. 从配置的 `models_dir` 扫描 GGUF，启动所选 `llama-server.exe`，逐批翻译成简体中文。
5. 输出中文字幕 SRT 或中英双语 SRT。
6. 可选用 FFmpeg 将中文字幕烧录到 MP4；双语模式使用 ASS，中文字号大于原文。
7. 支持一次选择多个视频并加入本地任务队列；GPU 重任务按顺序执行，避免多个 Whisper/llama 实例抢占显存。
8. Whisper 模型在同一配置下复用；页面提供“停止全部任务”，可取消排队任务并中止当前 FFmpeg/llama 子进程。

## 启动

双击 `start_webui.bat`，或在 PowerShell 中运行：

```powershell
.\start_webui.ps1
```

然后打开 <http://127.0.0.1:8877>。

首次运行如果当前 Python 环境没有 `faster-whisper`，启动脚本会安装 `requirements.txt`。启动脚本会优先复用可用的项目虚拟环境，否则再尝试 Codex 自带 Python。

## 配置

首次启动前，将 `config.example.json` 复制为 `config.json`，再按本机路径修改：

- `models_dir`：你的 GGUF 模型目录
- `llama_server`：CUDA 版 `llama-server.exe`
- `whisper_model`：本机 `faster-whisper-large-v3`
- `ffmpeg`：系统 PATH 中的 FFmpeg，或填写本地绝对路径
- `yt_dlp`：系统 PATH 中的 yt-dlp，或填写本地绝对路径（仅 URL 输入需要）
- `output_dir`：相对于 WebUI 目录的 `data`，实际任务会写入 `data\jobs\<任务号>`

模型下拉框会动态扫描指定目录中的 `.gguf`。默认优先展示 Hy-MT2 翻译模型，7B 模型更适合作为 16 GB 显存的首选；30B 和 Qwen GGUF 也可以手动选择。分片模型按标准 `-00001-of-xxxxx.gguf` 规则只展示一次，llama.cpp 会自动续载分片。多个视频可以连续提交，但为保证 16 GB 显存稳定性，队列默认只运行一个 GPU 任务。

## 依赖边界

- 必需：Python 3.12、`faster-whisper`、FFmpeg、CUDA 版 `llama-server.exe`。
- URL 输入额外需要 `yt-dlp`。
- `config.json` 不包含密码、Cookie 或令牌，不要把浏览器登录态放入交付包。

## 验收边界

应用会记录原文 SRT、任务日志和最终产物。生成文件、HTTP 成功、模型启动成功都只代表流水线执行证据，不代表字幕已经人工审核或已经公开发布。正式使用前建议先用 1–2 分钟视频检查专有名词、时间轴、字体和音频兼容性。
