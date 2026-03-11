#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
download_single.py — 单 DOI 论文下载脚本 (Playwright 版)
==========================================================
供 PaperHarvester 通过 subprocess 调用。
使用 Playwright 浏览器自动化访问 Sci-Hub 并下载 PDF。
首先尝试 headless 模式；若遇到验证码则切换为 headed 模式让用户手动处理。

用法: python download_single.py <DOI>
退出码: 0=下载成功, 1=下载失败

依赖: pip install playwright && python -m playwright install chromium
"""

import os
import re
import sys
import io
import time
import json
from pathlib import Path
from typing import Dict, Any

# === Windows GBK 编码兼容：强制 stdout/stderr 为 UTF-8 ===
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ╔══════════════════════════════════════════════════════════════════╗
# ║                          配 置                                   ║
# ╚══════════════════════════════════════════════════════════════════╝

# 默认 PDF 保存目录（本脚本所在目录），可通过命令行参数 --output-dir 覆盖
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent

SCIHUB_MIRRORS = [
    "https://sci-hub.se",
    "https://sci-hub.st",
    "https://sci-hub.ru",
    "https://sci-hub.ren",
]

# 页面加载超时（毫秒）
PAGE_TIMEOUT = 30000

# 等待 PDF 元素出现的超时（毫秒）
PDF_ELEMENT_TIMEOUT = 15000

# 下载超时（毫秒）
DOWNLOAD_TIMEOUT = 60000

# 最小有效 PDF 文件大小（字节），小于此值认为下载失败
MIN_PDF_SIZE = 5000

# 用户手动过验证码的最大等待时间（秒），仅在 headed 模式下使用
CAPTCHA_WAIT_TIMEOUT = 180

STATS_FILE = Path(__file__).resolve().parent / "mirror_stats.json"
SELECTORS_FILE = Path(__file__).resolve().parent / "custom_selectors.json"


# ╔══════════════════════════════════════════════════════════════════╗
# ║                      工 具 函 数                                ║
# ╚══════════════════════════════════════════════════════════════════╝

def get_sorted_mirrors() -> list:
    """根据历史成功次数对镜像进行降序排序"""
    stats: Dict[str, Any] = {}
    if STATS_FILE.exists():
        try:
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                stats = json.load(f)
        except Exception:
            pass
    
    # 按照成功次数降序排列，未记录的默认为 0
    return sorted(SCIHUB_MIRRORS, key=lambda m: stats.get(m, 0), reverse=True)

def record_mirror_success(mirror: str):
    """记录镜像成功下载一次"""
    stats: Dict[str, Any] = {}
    if STATS_FILE.exists():
        try:
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                stats = json.load(f)
        except Exception:
            pass
    
    stats[mirror] = int(stats.get(mirror, 0)) + 1
    
    try:
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(stats, f, indent=4)
    except Exception as e:
        print(f"    [WARN] 无法保存镜像统计数据: {e}")

def sanitize_doi_for_filename(doi: str) -> str:
    """将 DOI 转换为安全的文件名"""
    safe = doi.replace("/", "_")
    safe = re.sub(r'[<>:"|?*\\]', "_", safe)
    safe_str: str = safe
    if len(safe_str) > 200:
        safe = safe_str[:200]
    return safe


def validate_pdf(file_path: Path) -> bool:
    """验证文件是否为有效 PDF"""
    if not file_path.exists():
        return False
    if file_path.stat().st_size < MIN_PDF_SIZE:
        return False
    try:
        with open(file_path, "rb") as f:
            header = f.read(5)
        return header == b"%PDF-"
    except Exception:
        return False


def download_with_browser(page: Any, pdf_path: Path, timeout: int = DOWNLOAD_TIMEOUT) -> bool:
    """
    使用浏览器直接提取 PDF URL 并下载，避免由于显示内置PDF查看器导致的无法自动点击问题
    """
    pdf_url = None

    # 方法1：如果当前页面本身就是 PDF
    try:
        if page.url.lower().endswith('.pdf') or 'application/pdf' in page.evaluate('document.contentType || ""'):
            pdf_url = page.url
    except Exception:
        pass

    # 方法2：尝试从 embed 获取
    if not pdf_url:
        try:
            embed = page.query_selector('embed[type="application/pdf"]')
            if embed:
                pdf_url = embed.get_attribute('src')
        except Exception:
            pass

    # 方法3：尝试从 iframe 获取
    if not pdf_url:
        try:
            iframe = page.query_selector('iframe#pdf, iframe[src*=".pdf"]')
            if iframe:
                pdf_url = iframe.get_attribute('src')
        except Exception:
            pass

    # 方法4：尝试从 保存按钮 获取
    if not pdf_url:
        try:
            save_button = page.query_selector('button[onclick*="location.href"]')
            if save_button:
                onclick = save_button.get_attribute('onclick')
                import re
                m = re.search(r"location\.href\s*=\s*['\"]([^'\"]+)['\"]", onclick)
                if m:
                    pdf_url = m.group(1)
        except Exception:
            pass

    # 方法5：尝试从 a 标签 获取
    if not pdf_url:
        try:
            pdf_link = page.query_selector('a[href*=".pdf"]')
            if pdf_link:
                pdf_url = pdf_link.get_attribute('href')
        except Exception:
            pass

    if pdf_url:
        # 处理相对路径
        if pdf_url.startswith('//'):
            pdf_url = 'https:' + pdf_url
        elif pdf_url.startswith('/'):
            from urllib.parse import urlparse
            parsed = urlparse(page.url)
            pdf_url = f"{parsed.scheme}://{parsed.netloc}{pdf_url}"
        elif not pdf_url.startswith('http'):
            from urllib.parse import urljoin
            pdf_url = urljoin(page.url, pdf_url)

        pdf_url_str: str = str(pdf_url)
        print(f"    ⭐ [机器自动] 识别到PDF真实地址，开始后台强力下载...")
        print(f"       地址: {pdf_url_str[:80]}...")
        
        try:
            # 使用 Playwright 的内置 request context 下载，自动处理 cookie/UA
            response = page.context.request.get(pdf_url, timeout=timeout)
            if response.ok:
                with open(pdf_path, 'wb') as f:
                    f.write(response.body())
                if pdf_path.exists() and pdf_path.stat().st_size > MIN_PDF_SIZE:
                    return True
            else:
                print(f"    下载失败，服务器返回: {response.status}")
        except Exception as e:
            print(f"    请求PDF失败: {type(e).__name__}: {e}")

        # 如果直接请求失败，尝试旧方法的导航下载作为 fallback
        try:
            print("    尝试通过新标签页导航下载...")
            new_page = page.context.new_page()
            with new_page.expect_download(timeout=timeout) as download_info:
                # 触发保存对话框或在某些情况下直接下载
                new_page.goto(pdf_url, timeout=timeout)
            download = download_info.value
            download.save_as(pdf_path)
            new_page.close()
            if pdf_path.exists() and pdf_path.stat().st_size > MIN_PDF_SIZE:
                return True
        except Exception as e:
            print(f"    新标签页导航下载失败: {type(e).__name__}")
            try:
                new_page.close()
            except Exception:
                pass

    # 方法6：如果都没找到，尝试用户自定义的选择器 (点击)
    if SELECTORS_FILE.exists():
        try:
            with open(SELECTORS_FILE, "r", encoding="utf-8") as f:
                custom_selectors = json.load(f)
            
            for selector in custom_selectors:
                try:
                    btn = page.query_selector(selector)
                    if btn:
                        print(f"    ⭐ [机器代点] 识别到自定义按钮 ({selector})，正在自动点击...")
                        btn.evaluate("el => { el.style.border = '3px solid red'; el.style.backgroundColor = 'yellow'; el.style.transition = 'all 0.5s'; }")
                        page.wait_for_timeout(1500)
                        
                        # 特殊处理 built-in PDF viewer
                        if "pdf-viewer" in selector:
                            print("    [WARN] 选择器似乎是内置PDF浏览器，这通常不会触发下载，跳过拦截...")
                            # 不进行点击，因为点击往往只是选中区域不会下载
                            continue
                            
                        with page.expect_download(timeout=timeout) as download_info:
                            btn.click()
                        download = download_info.value
                        download.save_as(pdf_path)
                        if pdf_path.exists() and pdf_path.stat().st_size > MIN_PDF_SIZE:
                            return True
                except Exception as e:
                    print(f"    自定义按钮方式失败: {type(e).__name__}")
        except Exception:
            pass

    return False

def attempt_download_with_browser(
    doi: str,
    pdf_path: Path,
) -> int:
    """
    尝试下载论文。
    返回码:
    0 - 下载成功
    1 - 网络错误或超时 (建议重试)
    2 - 明确找不到该文献 (不要重试)
    """
    print(f"\n  [headed] 启动浏览器...")

    CHECK_INTERVAL = 1
    MAX_WAIT_TIME = 180

    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=False, channel="chrome")
            except Exception:
                browser = p.chromium.launch(headless=False)

            context = browser.new_context(
                accept_downloads=True,
                ignore_https_errors=True,
            )

            mirrors_to_try = get_sorted_mirrors()
            any_not_found = False
            for mirror_idx, mirror in enumerate(mirrors_to_try):
                scihub_url = f"{mirror}/{doi}"
                print(f"\n  [镜像 {mirror_idx + 1}/{len(mirrors_to_try)}] {mirror}")
                print(f"    访问: {scihub_url}")

                downloaded = False
                try:
                    page = context.new_page()
                    try:
                        # 核心访问逻辑
                        page.goto(scihub_url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")

                        wait_start = time.time()
                        pdf_ready = False

                        while time.time() - wait_start < MAX_WAIT_TIME:
                            try:
                                title = page.title()
                            except Exception:
                                title = ""

                            if "robot" in title.lower():
                                print("    [WARN] 需要验证，请手动完成...", end="\r")
                                time.sleep(CHECK_INTERVAL)
                                continue

                            # 增加对 Sci-Hub "Not Found" 样式的识别，避免傻等
                            page_text = page.inner_text("body").lower()
                            if "not available" in title.lower() or "alas, the following paper" in page_text:
                                print("\n    [SKIP] 镜像提示：该文献不在数据库中 (Not Found)")
                                any_not_found = True
                                break

                            # 检查是否有可下载内容
                            has_content = False
                            try:
                                # 尝试匹配常见内容
                                has_content = (
                                    page.url.lower().endswith('.pdf') or
                                    'application/pdf' in page.evaluate('document.contentType || ""') or
                                    page.locator('embed[type="application/pdf"]').count() > 0 or
                                    page.locator('iframe#pdf').count() > 0 or
                                    page.locator('a[href*=".pdf"]').count() > 0 or
                                    page.locator('button[onclick*="location.href"]').count() > 0
                                )
                                # 如果还没找到，尝试用户的自定义选择器
                                if not has_content and SELECTORS_FILE.exists():
                                    with open(SELECTORS_FILE, "r", encoding="utf-8") as f:
                                        custom_selectors = json.load(f)
                                    for selector in custom_selectors:
                                        if page.locator(selector).count() > 0:
                                            has_content = True
                                            break
                            except Exception:
                                pass

                            if has_content:
                                print("\n    🎯 找到下载目标节点！接管控制权中...")
                                pdf_ready = True
                                break

                            time.sleep(CHECK_INTERVAL)

                        # 尝试下载
                        if pdf_ready:
                            time.sleep(1)  # 等待页面稳定
                            if download_with_browser(page, pdf_path):
                                print(f"    [OK] 下载成功: {pdf_path.stat().st_size} bytes")
                                downloaded = True
                            else:
                                print(f"    [FAIL] 下载逻辑执行完毕但未成功保存文件")
                    finally:
                        try:
                            page.close()
                        except Exception:
                            pass

                    if downloaded:
                        record_mirror_success(mirror)
                        try:
                            browser.close()
                        except Exception:
                            pass
                        return 0

                except Exception as e:
                    print(f"    [FAIL] 发生错误: {type(e).__name__}: {e}")
                
                time.sleep(1)

            try:
                browser.close()
            except Exception:
                pass

    except Exception as e:
        print(f"  [FAIL] 浏览器启动失败: {type(e).__name__}: {e}")
        return 1

    if any_not_found:
        return 2
    return 1 # 如果所有镜像都尝试过了还没成功且没报 Not Found，可能是网络或其他未知错误

def download_paper(doi: str, output_dir: Path = DEFAULT_OUTPUT_DIR) -> int:
    """
    入口：直接且只使用与成功代码相同的 headed 模式
    """
    safe_name = sanitize_doi_for_filename(doi)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{safe_name}.pdf"

    if output_path.exists() and output_path.stat().st_size > 1000:
        print(f"  文件已存在且有效: {output_path.name}，跳过下载")
        return 0

    return attempt_download_with_browser(doi, output_path)

def main():
    import argparse
    parser = argparse.ArgumentParser(description="单 DOI 下载器 (Playwright)")
    parser.add_argument("doi", help="要下载的论文 DOI")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="保存目录")
    args = parser.parse_args()

    doi = args.doi.strip()
    output_dir = args.output_dir

    if not doi:
        print("错误: DOI 不能为空")
        sys.exit(1)

    print("=" * 60)
    print("download_single.py - 单 DOI 下载器 (Playwright)")
    print(f"DOI: {doi}")
    print(f"保存目录: {output_dir}")
    print("=" * 60)

    status = download_paper(doi, output_dir=output_dir)

    if status == 0:
        print(f"\n[OK] 下载完成")
        sys.exit(0)
    elif status == 2:
        print(f"\n[NOT_FOUND] 该文献在 Sci-Hub 中明确不存在")
        sys.exit(2)
    else:
        print(f"\n[ERROR] 下载过程中发生网络错误或未知异常")
        sys.exit(1)


if __name__ == "__main__":
    main()
