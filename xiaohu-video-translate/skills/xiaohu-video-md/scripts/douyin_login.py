#!/usr/bin/env python3
"""
抖音登录脚本 - 使用持久化浏览器上下文
首次使用需要手动登录，之后会自动保存登录状态
"""

import sys
import json
from pathlib import Path

try:
    from patchright.sync_api import sync_playwright
except ImportError:
    print("❌ 错误: 未安装 patchright")
    print("请运行: pip install patchright")
    sys.exit(1)


def douyin_login():
    """启动浏览器并等待用户手动登录抖音"""

    # 浏览器数据目录
    user_data_dir = Path(__file__).parent.parent / "data" / "browser_state" / "douyin_profile"

    print("🌟 启动Chrome浏览器，打开抖音登录页面...")

    # 使用真实Chrome和反检测配置
    browser_args = [
        '--disable-blink-features=AutomationControlled',  # 隐藏自动化特征
        '--disable-dev-shm-usage',
        '--no-sandbox',
        '--no-first-run',
        '--no-default-browser-check'
    ]

    user_agent = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

    with sync_playwright() as p:
        # 使用持久化上下文（保存浏览器状态和指纹）
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=False,
            channel="chrome",  # 使用真实Chrome
            no_viewport=True,  # 不设置固定视口
            ignore_default_args=["--enable-automation"],  # 移除自动化标识
            user_agent=user_agent,
            args=browser_args
        )

        # 使用默认页面
        page = context.pages[0] if context.pages else context.new_page()

        try:
            # 打开抖音首页
            douyin_url = "https://www.douyin.com/"
            print(f"📺 正在打开: {douyin_url}")
            page.goto(douyin_url, wait_until="domcontentloaded", timeout=60000)

            print("\n" + "="*60)
            print("✅ 浏览器已打开抖音页面！")
            print("="*60)
            print("\n👉 请在浏览器中手动登录抖音账号")
            print("   登录方式：")
            print("   - 手机号验证码登录")
            print("   - 扫码登录")
            print("   - 抖音账号密码登录")
            print("\n   💡 登录成功后，脚本会自动检测并保存状态")
            print("   ⏰ 脚本最多等待 2 分钟")
            print("="*60 + "\n")

            # 轮询检查登录状态
            import time
            max_wait_time = 120  # 最多等待 2 分钟
            start_time = time.time()

            print("🔍 检测登录状态中...")

            def is_logged_in(cookie_list):
                # 只看真正的会话 cookie，避免将访客态 ttwid 误判为已登录
                valid_names = {'sessionid', 'sessionid_ss', 'sid_guard'}
                for c in cookie_list:
                    name = c.get('name', '')
                    value = c.get('value', '')
                    domain = c.get('domain', '')
                    if name in valid_names and value and len(value) > 10 and 'douyin.com' in domain:
                        return True
                return False

            while True:
                # 检查登录状态
                cookies = context.cookies()
                session_cookies = [c for c in cookies if c.get('name') in ['sessionid', 'sessionid_ss', 'sid_guard']]

                if is_logged_in(session_cookies):
                    print(f"\n✅ 检测到登录成功！")
                    break

                # 检查超时
                elapsed = time.time() - start_time
                if elapsed >= max_wait_time:
                    print(f"\n⏰ 等待超时（{max_wait_time}秒），脚本退出")
                    print("   请重新运行脚本")
                    break

                # 等待 3 秒后再次检查
                time.sleep(3)
                print(f"   检测中... ({int(elapsed)}秒)", end='\r')

            # 提取cookies
            cookies = context.cookies()
            print(f"\n✅ 成功提取 {len(cookies)} 个cookies")

            # 保存cookies到文件（用于调试）
            cookies_file = Path(__file__).parent.parent / "data" / "tmp" / "douyin_cookies.json"
            cookies_file.parent.mkdir(parents=True, exist_ok=True)

            with open(cookies_file, 'w') as f:
                json.dump(cookies, f, indent=2)

            print(f"📁 Cookies已保存到: {cookies_file}")
            print(f"📁 浏览器状态已保存在: {user_data_dir}")

            # 检查关键cookies
            session_cookies = [c for c in cookies if c.get('name') in ['sessionid', 'sessionid_ss', 'sid_guard']]

            if session_cookies:
                print(f"\n✅ 登录成功！检测到 {len(session_cookies)} 个关键cookies")
                print("\n🔑 关键Cookies:")
                for cookie in session_cookies:
                    print(f"   - {cookie['name']}: {cookie['value'][:30]}...")
            else:
                print("\n⚠️  未检测到登录cookies，可能登录未成功")
                print("   请重新运行此脚本")

            print("\n" + "="*60)
            print("✅ 完成！浏览器已关闭")
            print("💡 下次使用时会自动使用本次的登录状态，无需重复登录")
            print("="*60)

        except Exception as e:
            print(f"❌ 错误: {e}")
            import traceback
            traceback.print_exc()

        finally:
            context.close()


if __name__ == "__main__":
    douyin_login()
