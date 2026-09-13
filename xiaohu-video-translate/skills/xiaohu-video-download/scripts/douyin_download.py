#!/usr/bin/env python3
"""
抖音资源下载脚本 - 监听API获取JSON，支持下载视频和音频
使用yt-dlp下载，需要先运行 douyin_login.py 登录
"""

import sys
import json
import time
import subprocess
import argparse
import re
import os
from pathlib import Path

try:
    from patchright.sync_api import sync_playwright
except ImportError:
    print("❌ 错误: 未安装 patchright")
    print("请运行: pip install patchright")
    sys.exit(1)


def generate_clean_filename(title, output_dir=None):
    """
    生成干净的文件名：提取前15个字符，去除所有符号，如有重复添加序号

    Args:
        title: 原始视频标题
        output_dir: 输出目录（用于检查重复文件）

    Returns:
        清理后的文件名（不含扩展名）
    """
    # 去除所有符号，保留中文、英文、数字
    clean = re.sub(r'[^\w\u4e00-\u9fff]', '', title)

    # 提取前15个字符
    base_name = clean[:15]

    # 如果提供了输出目录，检查是否有重复文件
    if output_dir:
        output_path = Path(output_dir)
        if output_path.exists():
            # 检查是否已存在同名文件
            existing_files = list(output_path.glob(f"{base_name}-音频.m4a"))

            if existing_files:
                # 有重复，添加序号
                counter = 2
                while True:
                    new_name = f"{base_name}_{counter}"
                    test_file = output_path / f"{new_name}-音频.m4a"
                    if not test_file.exists():
                        base_name = new_name
                        break
                    counter += 1

    return base_name


def get_video_info(json_file):
    """从JSON文件解析视频信息"""
    with open(json_file, 'r') as f:
        data = json.load(f)

    aweme_detail = data['aweme_detail']
    video_title = aweme_detail.get('desc', 'douyin_video')[:50]

    # 提取音频信息
    audio_url = None
    if 'video' in aweme_detail and 'bit_rate_audio' in aweme_detail['video']:
        audio_list = aweme_detail['video']['bit_rate_audio']
        if audio_list and len(audio_list) > 0:
            audio_meta = audio_list[0].get('audio_meta', {})
            url_list = audio_meta.get('url_list', {})
            audio_url = url_list.get('main_url') or url_list.get('backup_url')

    # 提取所有视频分辨率
    video_formats = []
    if 'bit_rate' in aweme_detail['video']:
        seen_resolutions = set()

        for br in aweme_detail['video']['bit_rate']:
            play_addr = br.get('play_addr', {})
            width = play_addr.get('width', 0)
            height = play_addr.get('height', 0)
            gear_name = play_addr.get('gear_name', 'unknown')
            data_size = play_addr.get('data_size', 0)

            if width == 0 or height == 0:
                continue

            resolution_key = f"{width}x{height}"
            if resolution_key in seen_resolutions:
                continue
            seen_resolutions.add(resolution_key)

            # 获取URL
            url_list = play_addr.get('url_list', [])
            if url_list and len(url_list) > 0:
                first_url = url_list[0]
                if isinstance(first_url, str):
                    video_url = first_url
                elif isinstance(first_url, dict):
                    video_url = first_url.get('main_url', '')
                else:
                    video_url = str(first_url) if first_url else ''

                if video_url:
                    video_formats.append({
                        'width': width,
                        'height': height,
                        'resolution': resolution_key,
                        'gear_name': gear_name,
                        'size_mb': data_size / 1024 / 1024 if data_size else 0,
                        'url': video_url
                    })

    # 按分辨率排序（从高到低）
    video_formats.sort(key=lambda x: (x['width'] * x['height']), reverse=True)

    return {
        'title': video_title,
        'audio_url': audio_url,
        'video_formats': video_formats
    }


def list_resources(video_info):
    """列出所有可用资源"""
    title = video_info['title']

    print(f"\n📺 视频: {title}")
    print(f"\n{'='*80}")
    print(f"{'视频选项':}")
    print(f"{'='*80}")

    if video_info['video_formats']:
        for i, fmt in enumerate(video_info['video_formats'], 1):
            resolution_name = get_resolution_name(fmt['width'], fmt['height'])
            print(f"{i}. {fmt['resolution']:12} ({resolution_name:8}) - {fmt['size_mb']:6.1f} MB")

    print(f"\n{'='*80}")
    print(f"{'音频选项':}")
    print(f"{'='*80}")
    print(f"a. 仅音频 - AAC格式, 约2.7MB")
    print(f"{'='*80}\n")


def get_resolution_name(width, height):
    """根据分辨率返回名称"""
    if width >= 3840:
        return "4K"
    elif width >= 2560:
        return "2K"
    elif width >= 1920:
        return "1080p"
    elif width >= 1280:
        return "720p"
    elif width >= 1024:
        return "540p"
    else:
        return "标清"


def download_with_ytdlp(url, output_file, referer="https://www.douyin.com/"):
    """使用yt-dlp下载资源"""
    cmd = [
        'yt-dlp',
        '--add-header', f'Referer:{referer}',
        '--no-warnings',
        '-o', str(output_file),
        url
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"❌ 下载失败: {result.stderr}")
        return False

    return True


def load_outdir(cli_outdir=None):
    """
    解析输出根目录，优先级：
    1) 命令行 --outdir（绝对路径）
    2) 本技能目录下 config.json 的 output_dir
    3) 无配置或无效时，交互提示用户输入绝对路径并写回 config.json
    """

    skill_root = Path(__file__).resolve().parent.parent
    config_path = skill_root / "config.json"
    example_path = skill_root / "config.example.json"

    def ensure_abs_dir(path_str):
        p = Path(os.path.expanduser(str(path_str)))
        if not p.is_absolute():
            return None
        p = p.resolve()
        if p.exists() and p.is_file():
            return None
        return p

    def read_config_output_dir():
        if not config_path.exists():
            return None
        try:
            cfg = json.loads(config_path.read_text("utf-8"))
        except Exception:
            return None
        if not isinstance(cfg, dict):
            return None
        v = cfg.get("output_dir")
        return str(v).strip() if isinstance(v, (str, int, float)) and str(v).strip() else None

    def write_config_output_dir(outdir: Path):
        # 尽量保留已有 config.json 的其他字段；若没有 __ui，则从 config.example.json 复制。
        cfg = {}
        if config_path.exists():
            try:
                obj = json.loads(config_path.read_text("utf-8"))
                if isinstance(obj, dict):
                    cfg = obj
            except Exception:
                cfg = {}

        cfg["output_dir"] = str(outdir)
        if not isinstance(cfg.get("settings"), dict):
            cfg["settings"] = {}
        if not isinstance(cfg.get("secrets"), dict):
            cfg["secrets"] = {}

        if not isinstance(cfg.get("__ui"), dict):
            try:
                ex = json.loads(example_path.read_text("utf-8")) if example_path.exists() else None
                if isinstance(ex, dict) and isinstance(ex.get("__ui"), dict):
                    cfg["__ui"] = ex.get("__ui")
            except Exception:
                pass

        text = json.dumps(cfg, ensure_ascii=False, indent=2) + "\n"
        config_path.write_text(text, encoding="utf-8")

    def prompt_outdir():
        if not sys.stdin.isatty():
            print("❌ 未找到有效的输出路径，且当前环境不支持交互。请使用 --outdir 或先在 config.json 中填写 output_dir（绝对路径）。")
            sys.exit(1)
        while True:
            user_input = input("请输入输出根目录（绝对路径，如 /Users/xxx/Downloads）：").strip()
            if not user_input:
                print("❌ 路径不能为空，请重新输入。")
                continue
            path = ensure_abs_dir(user_input)
            if not path:
                print("❌ 必须是绝对路径且不能指向现有文件，请重新输入。")
                continue
            path.mkdir(parents=True, exist_ok=True)
            try:
                write_config_output_dir(path)
                print(f"✅ 已保存输出根目录到 {config_path}")
            except Exception as e:
                print(f"⚠️  写入 {config_path} 失败：{e}")
            return path

    if cli_outdir:
        path = ensure_abs_dir(cli_outdir)
        if not path:
            print("❌ --outdir 必须是绝对路径且不能指向文件。")
            sys.exit(1)
        return path

    outdir_value = read_config_output_dir()
    if outdir_value:
        path = ensure_abs_dir(outdir_value)
        if path:
            return path
        print("⚠️ 检测到 config.json 中的 output_dir 无效，将引导重新填写。")
        return prompt_outdir()

    print("⚠️ 未在 config.json 找到 output_dir，将引导设置输出目录（绝对路径）。")
    return prompt_outdir()


def standardize_url(video_url: str) -> str:
    """将各种抖音链接规范为 https://www.douyin.com/video/<id>"""
    if "modal_id=" in video_url:
        import urllib.parse as up
        parsed = up.urlparse(video_url)
        qs = up.parse_qs(parsed.query)
        mid = qs.get("modal_id", [None])[0]
        if mid:
            return f"https://www.douyin.com/video/{mid}"
    # 简单提取末尾数字ID
    import re
    m = re.search(r'/video/(\\d+)', video_url)
    if m:
        return f"https://www.douyin.com/video/{m.group(1)}"
    return video_url


def download_douyin_resources(video_url, tmp_dir, mode='video', resolution=None, final_dir=None):
    """下载抖音资源的主函数

    Args:
        video_url: 抖音视频 URL
        tmp_dir: 临时目录（保存 aweme_detail.json 等）
        mode: video/audio/both/list
        resolution: 期望分辨率
        final_dir: 最终输出目录（保存 mp4/m4a）。不传则与 tmp_dir 相同（向后兼容）
    """

    # 浏览器数据目录
    user_data_dir = Path(__file__).parent.parent / "data" / "browser_state" / "douyin_profile"

    if not user_data_dir.exists():
        print("❌ 错误: 未检测到浏览器登录状态")
        print(f"   请先运行: python {Path(__file__).parent / 'douyin_login.py'}")
        return False

    tmp_dir = Path(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    if final_dir is None:
        final_dir = tmp_dir
    final_dir = Path(final_dir)
    final_dir.mkdir(parents=True, exist_ok=True)

    # 反检测配置
    browser_args = [
        '--disable-blink-features=AutomationControlled',
        '--disable-dev-shm-usage',
        '--no-sandbox',
        '--no-first-run',
        '--no-default-browser-check'
    ]

    user_agent = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

    print("🌟 启动浏览器并监听API响应...")

    api_data = None
    video_title = "douyin_video"

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=False,  # 使用可见窗口以降低风控，抓取 aweme detail
            channel="chrome",
            no_viewport=True,
            ignore_default_args=["--enable-automation"],
            user_agent=user_agent,
            args=browser_args
        )

        page = context.pages[0] if context.pages else context.new_page()

        api_count = 0

        def handle_response(response):
            nonlocal api_data, video_title, api_count

            if 'aweme' in response.url:
                api_count += 1
                print(f"  📍 API #{api_count}: {response.url[:80]}...")

            if 'aweme/v1/web/aweme/detail/' in response.url:
                try:
                    data = response.json()
                    print(f"  ✅ 捕获目标API响应！")

                    if 'aweme_detail' in data:
                        aweme_detail = data['aweme_detail']
                        if 'desc' in aweme_detail:
                            video_title = aweme_detail['desc'][:50]

                    api_data = data
                except Exception as e:
                    print(f"  ⚠️  解析API响应失败: {e}")

        page.on("response", handle_response)

        try:
            std_url = standardize_url(video_url)
            print(f"📺 正在打开: {std_url}")
            page.goto(std_url, wait_until="domcontentloaded", timeout=60000)

            print(f"📄 页面标题: {page.title()}")
            print(f"📄 页面 URL: {page.url}")

            def is_aweme_detail(resp):
                return 'aweme/v1/web/aweme/detail' in resp.url

            print("⏳ 等待API响应（最多60秒，必要时刷新重试）...")
            try:
                resp = page.wait_for_event("response", is_aweme_detail, timeout=60000)
                print(f"  ✅ 捕获目标API响应！status={resp.status}")
                try:
                    data = resp.json()
                except Exception as e_json:
                    print(f"  ⚠️  解析JSON失败，尝试读取文本: {e_json}")
                    try:
                        txt = resp.text()
                        print(f"  ⚠️  detail 文本长度: {len(txt)}")
                        data = json.loads(txt) if txt else None
                    except Exception as e_txt:
                        print(f"  ⚠️  读取文本失败: {e_txt}")
                        data = None
                if data and 'aweme_detail' in data:
                    aweme_detail = data['aweme_detail']
                    if 'desc' in aweme_detail:
                        video_title = aweme_detail['desc'][:50]
                    api_data = data
            except Exception as e:
                print(f"  ⚠️  首次等待 detail 失败: {e}")

            # 兜底：未抓到则刷新再等 30 秒
            if not api_data:
                print("🔄 未捕获到 aweme detail，尝试刷新再等待 30 秒...")
                try:
                    page.reload(wait_until="domcontentloaded", timeout=60000)
                    resp = page.wait_for_event("response", is_aweme_detail, timeout=30000)
                    print(f"  ✅ 捕获目标API响应（刷新后）！status={resp.status}")
                    try:
                        data = resp.json()
                    except Exception as e_json:
                        print(f"  ⚠️  解析JSON失败，尝试读取文本: {e_json}")
                        try:
                            txt = resp.text()
                            print(f"  ⚠️  detail 文本长度: {len(txt)}")
                            data = json.loads(txt) if txt else None
                        except Exception as e_txt:
                            print(f"  ⚠️  读取文本失败: {e_txt}")
                            data = None
                    if data and 'aweme_detail' in data:
                        aweme_detail = data['aweme_detail']
                        if 'desc' in aweme_detail:
                            video_title = aweme_detail['desc'][:50]
                        api_data = data
                except Exception as e:
                    print(f"  ⚠️  刷新后仍未捕获 detail: {e}")

            if not api_data:
                print("❌ 未捕获到API响应数据")
                print("可能的原因：")
                print("  1. 登录状态已过期，请重新运行登录脚本")
                print("  2. 视频链接无效或视频不存在")
                print("  3. 网络连接问题")
                return False

            # 保存 API 响应数据（临时）
            json_file = tmp_dir / "aweme_detail.json"
            with open(json_file, 'w') as f:
                json.dump(api_data, f, indent=2, ensure_ascii=False)
            print(f"💾 已保存API数据: {json_file}")

            # 解析视频信息
            print(f"\n🔍 解析视频信息...")
            video_info = get_video_info(json_file)

            # 如果只是列出资源
            if mode == 'list':
                list_resources(video_info)
                return True

            # 下载逻辑
            # 🔥 使用新的智能文件命名函数
            clean_title = generate_clean_filename(video_info['title'], final_dir)

            # 显示生成的文件名
            print(f"📝 生成文件名: {clean_title}-音频.m4a")

            if mode == 'audio':
                # 仅下载音频
                if not video_info['audio_url']:
                    print("❌ 未找到音频URL")
                    return False

                audio_file = final_dir / f"{clean_title}-音频.m4a"
                print(f"\n📥 正在下载音频...")
                print(f"   保存到: {audio_file}")
                print(f"   音频URL: {video_info['audio_url'][:100]}...")

                try:
                    if download_with_ytdlp(video_info['audio_url'], audio_file):
                        size_mb = audio_file.stat().st_size / 1024 / 1024
                        print(f"✅ 音频已下载: {audio_file}")
                        print(f"📁 文件大小: {size_mb:.2f} MB")
                        return True
                    else:
                        print("❌ 下载函数返回False")
                        return False
                except Exception as e:
                    print(f"❌ 下载过程出错: {e}")
                    import traceback
                    traceback.print_exc()
                    return False

            elif mode == 'video' or mode == 'both':
                # 下载视频
                if not video_info['video_formats']:
                    print("❌ 未找到视频URL")
                    return False

                # 选择分辨率
                if resolution:
                    # 用户指定的分辨率
                    selected_fmt = None
                    for fmt in video_info['video_formats']:
                        if resolution.lower() in fmt['resolution'].lower() or \
                           resolution.lower() in get_resolution_name(fmt['width'], fmt['height']).lower():
                            selected_fmt = fmt
                            break

                    if not selected_fmt:
                        print(f"❌ 未找到指定分辨率: {resolution}")
                        print(f"   可用分辨率: {[f['resolution'] for f in video_info['video_formats']]}")
                        return False
                else:
                    # 默认选择最高清
                    selected_fmt = video_info['video_formats'][0]

                video_file = final_dir / f"{clean_title}-{selected_fmt['resolution']}.mp4"
                print(f"\n📥 正在下载视频...")
                print(f"   分辨率: {selected_fmt['resolution']} ({get_resolution_name(selected_fmt['width'], selected_fmt['height'])})")
                print(f"   保存到: {video_file}")

                if download_with_ytdlp(selected_fmt['url'], video_file):
                    size_mb = video_file.stat().st_size / 1024 / 1024
                    print(f"✅ 视频已下载: {video_file}")
                    print(f"📁 文件大小: {size_mb:.2f} MB")

                    # 如果同时下载音频
                    if mode == 'both' and video_info['audio_url']:
                        audio_file = final_dir / f"{clean_title}-音频.m4a"
                        print(f"\n📥 正在下载音频...")
                        print(f"   保存到: {audio_file}")

                        if download_with_ytdlp(video_info['audio_url'], audio_file):
                            size_mb = audio_file.stat().st_size / 1024 / 1024
                            print(f"✅ 音频已下载: {audio_file}")
                            print(f"📁 文件大小: {size_mb:.2f} MB")

                    return True
                return False

        except Exception as e:
            print(f"❌ 错误: {e}")
            import traceback
            traceback.print_exc()
            return False

        finally:
            context.close()


def main():
    parser = argparse.ArgumentParser(
        description='抖音资源下载脚本 - 支持下载视频和音频',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
使用示例:
  # 下载最高清视频（默认）
  python douyin_download.py <URL>

  # 下载指定分辨率视频
  python douyin_download.py <URL> --resolution 1080p

  # 仅下载音频
  python douyin_download.py <URL> --audio

  # 同时下载视频和音频
  python douyin_download.py <URL> --both

  # 仅列出可用资源
  python douyin_download.py <URL> --list

分辨率选项:
  4k, 2k, 1080p, 720p, 540p
        '''
    )

    parser.add_argument('url', help='抖音视频URL')
    parser.add_argument('--audio', action='store_true', help='仅下载音频')
    parser.add_argument('--video', action='store_true', help='仅下载视频（默认行为）')
    parser.add_argument('--both', action='store_true', help='同时下载视频和音频')
    parser.add_argument('--resolution', help='视频分辨率 (4k/2k/1080p/720p/540p)，默认4k')
    parser.add_argument('--list', action='store_true', help='仅列出可用资源，不下载')
    parser.add_argument('--outdir', help='自定义输出根目录（绝对路径；优先于 config.json 的 output_dir）')

    args = parser.parse_args()

    # 确定模式
    if args.list:
        mode = 'list'
    elif args.audio:
        mode = 'audio'
    elif args.both:
        mode = 'both'
    else:
        mode = 'video'  # 默认

    # 输出目录
    outdir_root = load_outdir(args.outdir)
    outdir_root.mkdir(parents=True, exist_ok=True)
    tmp_dir = outdir_root / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    final_dir = outdir_root / "data"
    final_dir.mkdir(parents=True, exist_ok=True)
    print(f"📂 输出根目录: {outdir_root}")
    print(f"📂 临时目录: {tmp_dir}")
    print(f"📂 最终目录: {final_dir}")

    # 执行下载
    success = download_douyin_resources(
        args.url,
        tmp_dir,
        mode=mode,
        resolution=args.resolution,
        final_dir=final_dir,
    )

    if success:
        print(f"\n✅ 操作成功完成")
        sys.exit(0)
    else:
        print(f"\n❌ 操作失败")
        sys.exit(1)


if __name__ == "__main__":
    main()
