#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
论文下载脚本 - 混合模式（使用浏览器原生下载）
用户通过人机验证后，脚本自动点击保存按钮下载

关键改进：使用 Playwright 原生下载功能，而非 requests
"""

import os
import csv
import re
import sys
import json
import time
from pathlib import Path
from datetime import datetime
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ==================== 配置区域 ====================
SCRIPT_DIR = Path(__file__).parent
DOI_CSV_PATH = SCRIPT_DIR / "dois.csv"
OUTPUT_DIR = Path(r"C:\Users\13081\Desktop\一体化设计\相关文献\doi2paper-main\doi2paper-main\putput")
PROGRESS_FILE = SCRIPT_DIR / "download_progress_hybrid.json"
LOG_FILE = SCRIPT_DIR / "download_log_hybrid.txt"

SCIHUB_MIRRORS = [
    "https://sci-hub.st",
    "https://sci-hub.ru",
]

PAGE_TIMEOUT = 30000
DOWNLOAD_TIMEOUT = 60000
CHECK_INTERVAL = 1
MAX_WAIT_TIME = 180
# ==================== 配置结束 ====================


class DownloadProgress:
    def __init__(self, progress_file: Path):
        self.progress_file = progress_file
        self.data = {"downloaded": [], "failed": [], "last_update": None}
        self.load()
    
    def load(self):
        if self.progress_file.exists():
            try:
                with open(self.progress_file, 'r', encoding='utf-8') as f:
                    self.data.update(json.load(f))
                print(f"已加载进度: 成功 {len(self.data['downloaded'])}, 失败 {len(self.data['failed'])}")
            except Exception:
                pass
    
    def save(self):
        self.data["last_update"] = datetime.now().isoformat()
        with open(self.progress_file, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
    
    def is_processed(self, doi: str) -> bool:
        return doi in self.data["downloaded"] or doi in self.data["failed"]
    
    def mark_downloaded(self, doi: str):
        if doi not in self.data["downloaded"]:
            self.data["downloaded"].append(doi)
        if doi in self.data["failed"]:
            self.data["failed"].remove(doi)
        self.save()
    
    def mark_failed(self, doi: str):
        if doi not in self.data["failed"] and doi not in self.data["downloaded"]:
            self.data["failed"].append(doi)
        self.save()
    
    def get_stats(self):
        return len(self.data["downloaded"]), len(self.data["failed"])


def log_message(message: str):
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")


def sanitize_filename(filename: str) -> str:
    filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
    return filename[:200] if len(filename) > 200 else filename


def extract_valid_dois(csv_path: Path) -> list:
    valid_dois = []
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            doi = row.get('doi', '').strip()
            if doi and doi.lower() != 'no doi':
                valid_dois.append(doi)
    return valid_dois


def get_pdf_count(output_dir: Path) -> int:
    return len(list(output_dir.glob("*.pdf"))) if output_dir.exists() else 0


def download_with_browser(page, pdf_path: Path, timeout: int = DOWNLOAD_TIMEOUT) -> bool:
    """
    使用浏览器原生功能下载 PDF
    尝试多种方式：点击保存按钮、点击 PDF 链接、导航到 PDF URL
    """
    try:
        # 方法1：查找并点击保存按钮
        save_button = page.query_selector('button[onclick*="location.href"]')
        if save_button:
            print("  点击保存按钮...")
            with page.expect_download(timeout=timeout) as download_info:
                save_button.click()
            download = download_info.value
            download.save_as(pdf_path)
            if pdf_path.exists() and pdf_path.stat().st_size > 1000:
                return True
    except Exception as e:
        print(f"  保存按钮方式失败: {type(e).__name__}")
    
    try:
        # 方法2：查找 PDF 链接并点击
        pdf_link = page.query_selector('a[href*=".pdf"]')
        if pdf_link:
            print("  点击PDF链接...")
            with page.expect_download(timeout=timeout) as download_info:
                pdf_link.click()
            download = download_info.value
            download.save_as(pdf_path)
            if pdf_path.exists() and pdf_path.stat().st_size > 1000:
                return True
    except Exception as e:
        print(f"  PDF链接方式失败: {type(e).__name__}")
    
    try:
        # 方法3：从 embed/iframe 获取 src 并导航
        pdf_url = None
        embed = page.query_selector('embed[type="application/pdf"]')
        if embed:
            pdf_url = embed.get_attribute('src')
        if not pdf_url:
            iframe = page.query_selector('iframe#pdf, iframe[src*=".pdf"]')
            if iframe:
                pdf_url = iframe.get_attribute('src')
        
        if pdf_url:
            # 处理相对路径
            if pdf_url.startswith('//'):
                pdf_url = 'https:' + pdf_url
            elif pdf_url.startswith('/'):
                # 从当前页面URL获取域名
                current_url = page.url
                from urllib.parse import urlparse
                parsed = urlparse(current_url)
                pdf_url = f"{parsed.scheme}://{parsed.netloc}{pdf_url}"
            
            print(f"  导航到PDF: {pdf_url[:60]}...")
            
            # 创建新标签页下载，避免影响当前页面
            new_page = page.context.new_page()
            try:
                with new_page.expect_download(timeout=timeout) as download_info:
                    new_page.goto(pdf_url, timeout=timeout)
                download = download_info.value
                download.save_as(pdf_path)
                new_page.close()
                if pdf_path.exists() and pdf_path.stat().st_size > 1000:
                    return True
            except Exception as e:
                print(f"  新标签页下载失败: {type(e).__name__}")
                new_page.close()
                
                # 如果没触发下载，可能是直接显示PDF，尝试用 iframe 内容
                try:
                    # 尝试从 iframe 获取 PDF 内容
                    frame = page.frame_locator('#pdf').first
                    if frame:
                        print("  尝试从iframe获取...")
                except Exception:
                    pass
    except Exception as e:
        print(f"  embed/iframe方式失败: {type(e).__name__}")
    
    return False


def main():
    print("=" * 60)
    print("论文下载工具 - 混合模式（浏览器原生下载）")
    print("=" * 60)
    print("流程：")
    print("  1. 自动打开 Sci-Hub 页面")
    print("  2. 如有人机验证，请手动完成")
    print("  3. 验证通过后自动下载并切换下一个")
    print("  4. Ctrl+C 退出")
    print("=" * 60)
    
    if not DOI_CSV_PATH.exists():
        print(f"错误: 未找到 {DOI_CSV_PATH}")
        sys.exit(1)
    
    progress = DownloadProgress(PROGRESS_FILE)
    dois = extract_valid_dois(DOI_CSV_PATH)
    print(f"找到 {len(dois)} 个DOI")
    
    pending_dois = [doi for doi in dois if not progress.is_processed(doi)]
    print(f"待处理: {len(pending_dois)} 篇")
    
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"输出目录: {OUTPUT_DIR} (当前 {get_pdf_count(OUTPUT_DIR)} 个PDF)")
    
    if len(pending_dois) == 0:
        print("\n所有论文已处理！")
        sys.exit(0)
    
    input("\n按 Enter 开始...")
    
    success_count, fail_count = progress.get_stats()
    
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False, channel='chrome')
            context = browser.new_context(
                accept_downloads=True, 
                ignore_https_errors=True,
            )
            
            print("\n浏览器已启动")
            
            for i, doi in enumerate(pending_dois):
                safe_doi = sanitize_filename(doi)
                pdf_path = OUTPUT_DIR / f"{safe_doi}.pdf"
                
                # 跳过已存在的文件
                if pdf_path.exists() and pdf_path.stat().st_size > 1000:
                    print(f"\n[{i+1}/{len(pending_dois)}] {doi} - 已存在，跳过")
                    progress.mark_downloaded(doi)
                    success_count += 1
                    continue
                
                downloaded = False
                
                for mirror in SCIHUB_MIRRORS:
                    url = f"{mirror}/{doi}"
                    print(f"\n[{i+1}/{len(pending_dois)}] {doi}")
                    print(f"  访问: {url}")
                    
                    try:
                        page = context.new_page()
                        page.goto(url, timeout=PAGE_TIMEOUT, wait_until="networkidle")
                        
                        # 等待页面加载完成或用户通过验证
                        wait_start = time.time()
                        pdf_ready = False
                        
                        while time.time() - wait_start < MAX_WAIT_TIME:
                            title = page.title()
                            
                            if "robot" in title.lower():
                                print("  ⚠ 需要验证，请手动完成...", end="\r")
                                time.sleep(CHECK_INTERVAL)
                                continue
                            
                            if "not available" in title.lower():
                                print("  ✗ 论文不可用")
                                break
                            
                            # 检查是否有可下载内容
                            has_content = (
                                page.query_selector('embed[type="application/pdf"]') or
                                page.query_selector('iframe#pdf') or
                                page.query_selector('a[href*=".pdf"]') or
                                page.query_selector('button[onclick*="location.href"]')
                            )
                            
                            if has_content:
                                print("  检测到PDF内容")
                                pdf_ready = True
                                break
                            
                            time.sleep(CHECK_INTERVAL)
                        
                        # 尝试下载
                        if pdf_ready:
                            time.sleep(1)  # 等待页面稳定
                            if download_with_browser(page, pdf_path):
                                print(f"  ✓ 下载成功: {pdf_path.stat().st_size} bytes")
                                downloaded = True
                        
                        page.close()
                        
                        if downloaded:
                            break
                            
                    except Exception as e:
                        print(f"  错误: {type(e).__name__}")
                        try:
                            page.close()
                        except Exception:
                            pass
                
                # 记录结果
                if downloaded:
                    progress.mark_downloaded(doi)
                    success_count += 1
                    log_message(f"成功: {doi}")
                else:
                    progress.mark_failed(doi)
                    fail_count += 1
                    log_message(f"失败: {doi}")
                
                print(f"  统计: 成功={success_count}, 失败={fail_count}")
                time.sleep(0.5)
            
            browser.close()
    
    except KeyboardInterrupt:
        print("\n\n用户中断")
    
    success_count, fail_count = progress.get_stats()
    print(f"\n最终进度: 成功={success_count}, 失败={fail_count}, PDF={get_pdf_count(OUTPUT_DIR)}")


if __name__ == "__main__":
    main()
