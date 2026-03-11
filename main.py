#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
PaperHarvester — 自动化文献滚雪球检索与下载系统
=================================================
从种子 DOI 出发，提取参考文献，使用 DeepSeek API 评估相关性，
调用本地脚本下载 PDF，循环往复直到达到目标下载数量。

依赖安装：pip install requests tenacity python-dotenv rich
"""

import os
import re
import sys
import io
import json
import time
import sqlite3
import logging
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple, List, Dict, Any

# === Windows GBK 编码兼容：强制 stdout/stderr 为 UTF-8 ===
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import requests
from requests.exceptions import HTTPError
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception,
    before_sleep_log,
)
from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table
from rich.panel import Panel

# ── 自定义异常 ──
class MetadataTransientError(Exception):
    """网络超时或临时服务器错误，应保留 pending 状态重试。"""
    pass

class MetadataNotFoundError(Exception):
    """明确找不到文献（404），标记为 failed。"""
    pass
def _is_retryable_error(exception: BaseException) -> bool:
    """
    自定义重试判断：只重试暂时性错误，跳过永久性错误。
    - 401 (Unauthorized)、403 (Forbidden)、404 (Not Found) → 不重试
    - 429 (Rate Limit)、5xx (服务器错误) → 重试
    - 超时、连接错误等网络异常 → 重试
    """
    if isinstance(exception, HTTPError):
        response: Any = getattr(exception, "response", None)
        if response is not None:
            code = getattr(response, "status_code", 500)
            # 永久性客户端错误，不重试
            if code in (401, 403, 404):
                return False
            # 429 或 5xx，值得重试
            return code == 429 or code >= 500
        return True  # 没有 response，重试
    # 其他网络异常（Timeout, ConnectionError 等），重试
    if isinstance(exception, (requests.Timeout, requests.ConnectionError)):
        return True
    # requests 的其他异常
    if isinstance(exception, requests.RequestException):
        return True
    return False

# ╔══════════════════════════════════════════════════════════════════╗
# ║              从 config.json 加载配置                              ║
# ╚══════════════════════════════════════════════════════════════════╝

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"

def _load_config() -> Dict[str, Any]:
    """从 config.json 加载配置，如不存在则返回默认值"""
    defaults: Dict[str, Any] = {
        "topic_description": "火箭发动机推力室一体化设计、再生冷却与增材制造",
        "target_download_count": 50,
        "seed_dois": ["10.2514/1.B38364", "10.1016/j.actaastro.2021.01.032"],
        "paths": {
            "todo_dir": "todo", "output_dir": "output",
            "db_file": "papers.db", "download_script": "src/download_single.py",
            "env_file": ".env"
        },
        "api": {
            "semantic_scholar": "https://api.semanticscholar.org/graph/v1",
            "crossref": "https://api.crossref.org/works",
            "deepseek_url": "https://api.deepseek.com/v1/chat/completions",
            "deepseek_model": "deepseek-chat"
        },
        "timeouts": {
            "http_timeout_sec": 30,
            "download_subprocess_timeout_sec": 300,
            "api_call_interval_sec": 1.5,
            "max_runtime_sec": 3600,
            "inactivity_timeout_sec": 600
        },
        "snowball": {
            "enabled": True,
            "extract_from_downloaded_pdf": True,
            "max_depth": 3
        }
    }
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
            # 深度合并
            user_cfg_dict: Dict[str, Any] = user_cfg
            for k, v in user_cfg_dict.items():
                kn: str = str(k)
                if isinstance(v, dict) and kn in defaults and isinstance(defaults.get(kn), dict):
                    d_val = defaults[kn]
                    if isinstance(d_val, dict):
                        d_val.update(v)
                else:
                    defaults[kn] = v
        except Exception as e:
            print(f"[WARN] config.json 解析失败，使用默认配置: {e}")
    return defaults

CFG = _load_config()

# ── 从配置中提取常量 ──
TOPIC_DESCRIPTION: str     = CFG["topic_description"]
TARGET_DOWNLOAD_COUNT: int = CFG["target_download_count"]
SEED_DOIS: list            = CFG["seed_dois"]

TODO_DIR       = BASE_DIR / CFG["paths"]["todo_dir"]
OUTPUT_DIR     = BASE_DIR / CFG["paths"]["output_dir"]
DB_PATH        = BASE_DIR / CFG["paths"]["db_file"]
DOWNLOAD_SCRIPT = BASE_DIR / CFG["paths"]["download_script"]
ENV_PATH       = BASE_DIR / CFG["paths"]["env_file"]

SEMANTIC_SCHOLAR_API = CFG["api"]["semantic_scholar"]
CROSSREF_API         = CFG["api"]["crossref"]
DEEPSEEK_API_URL     = CFG["api"]["deepseek_url"]
DEEPSEEK_MODEL       = CFG["api"]["deepseek_model"]

HTTP_TIMEOUT                = CFG["timeouts"]["http_timeout_sec"]
DOWNLOAD_SUBPROCESS_TIMEOUT = CFG["timeouts"]["download_subprocess_timeout_sec"]
API_CALL_INTERVAL           = CFG["timeouts"]["api_call_interval_sec"]
MAX_RUNTIME_SEC             = CFG["timeouts"]["max_runtime_sec"]
INACTIVITY_TIMEOUT_SEC      = CFG["timeouts"]["inactivity_timeout_sec"]

SNOWBALL_ENABLED            = CFG["snowball"]["enabled"]
SNOWBALL_FROM_PDF           = CFG["snowball"]["extract_from_downloaded_pdf"]
SNOWBALL_MAX_DEPTH          = CFG["snowball"]["max_depth"]

DEFAULT_SEED_DOIS = SEED_DOIS  # 兼容别名

# ╔══════════════════════════════════════════════════════════════════╗
# ║                      日 志 初 始 化                              ║
# ╚══════════════════════════════════════════════════════════════════╝

console = Console()

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(console=console, rich_tracebacks=True, markup=True)],
)
logger = logging.getLogger("PaperHarvester")


# ╔══════════════════════════════════════════════════════════════════╗
# ║               模块 1：持久化层  PaperDatabase                    ║
# ╚══════════════════════════════════════════════════════════════════╝

class PaperDatabase:
    """
    SQLite 持久化层。
    所有写操作即时 commit，保证断点续传绝对可靠。
    状态流转：pending -> evaluated / downloaded / failed
    """

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.conn = sqlite3.connect(str(db_path), timeout=10)
        self.conn.execute("PRAGMA journal_mode=WAL")  # WAL 模式防止锁冲突
        self.conn.execute("PRAGMA busy_timeout=5000") # 锁等待 5 秒，防止 database is locked
        self._create_table()

    def _create_table(self):
        """创建 papers 表（如不存在）"""
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS papers (
                doi         TEXT PRIMARY KEY,
                title       TEXT DEFAULT '',
                abstract    TEXT DEFAULT '',
                relevance_score INTEGER DEFAULT 0,
                status      TEXT DEFAULT 'pending',
                depth       INTEGER DEFAULT 0,
                added_at    TEXT DEFAULT '',
                updated_at  TEXT DEFAULT ''
            )
        """)
        self.conn.commit()

    def add_doi(self, doi: str, depth: int = 0) -> bool:
        """
        添加新 DOI 到数据库（状态为 pending）。
        如果 DOI 已存在则静默忽略，返回 False。
        """
        doi = doi.strip()
        if not doi:
            return False
        now = datetime.now().isoformat()
        try:
            self.conn.execute(
                "INSERT INTO papers (doi, status, depth, added_at, updated_at) VALUES (?, 'pending', ?, ?, ?)",
                (doi, depth, now, now),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            # DOI 已存在，静默忽略
            return False

    def add_dois_batch(self, dois: List[str], depth: int = 0) -> int:
        """
        批量添加 DOI 列表，返回实际新增数量。
        """
        added_count: int = 0
        now = datetime.now().isoformat()
        for doi in dois:
            doi = doi.strip()
            if not doi:
                continue
            try:
                self.conn.execute(
                    "INSERT INTO papers (doi, status, depth, added_at, updated_at) VALUES (?, 'pending', ?, ?, ?)",
                    (doi, depth, now, now),
                )
                added_count = int(added_count) + 1
            except sqlite3.IntegrityError:
                continue
        self.conn.commit()
        return added_count

    def get_next_pending(self) -> Optional[str]:
        """
        获取下一个状态为 pending 的 DOI。
        按添加时间排序，优先处理较早的条目。
        返回 DOI 字符串或 None（队列为空时）。
        """
        cursor = self.conn.execute(
            "SELECT doi FROM papers WHERE status = 'pending' ORDER BY depth ASC, updated_at ASC LIMIT 1"
        )
        row = cursor.fetchone()
        return row[0] if row else None

    def update_paper(
        self,
        doi: str,
        title: Optional[str] = None,
        abstract: Optional[str] = None,
        relevance_score: Optional[int] = None,
        status: Optional[str] = None,
    ):
        """更新指定 DOI 的字段，仅更新非 None 的参数。"""
        fields: List[str] = []
        values: List[Any] = []
        if title is not None:
            fields.append("title = ?")
            values.append(title)
        if abstract is not None:
            fields.append("abstract = ?")
            values.append(abstract)
        if relevance_score is not None:
            fields.append("relevance_score = ?")
            values.append(relevance_score)
        if status is not None:
            fields.append("status = ?")
            values.append(status)
        if not fields:
            return
        fields.append("updated_at = ?")
        values.append(datetime.now().isoformat())
        values.append(doi)
        sql = f"UPDATE papers SET {', '.join(fields)} WHERE doi = ?"
        self.conn.execute(sql, values)
        self.conn.commit()

    def count_downloaded(self) -> int:
        """统计已成功下载的论文总数。"""
        cursor = self.conn.execute(
            "SELECT COUNT(*) FROM papers WHERE status = 'downloaded'"
        )
        return cursor.fetchone()[0]

    def count_by_status(self) -> dict:
        """按状态统计各类论文数量。"""
        cursor = self.conn.execute(
            "SELECT status, COUNT(*) FROM papers GROUP BY status"
        )
        return {row[0]: row[1] for row in cursor.fetchall()}

    def total_count(self) -> int:
        """数据库中论文总数。"""
        cursor = self.conn.execute("SELECT COUNT(*) FROM papers")
        return cursor.fetchone()[0]

    def close(self):
        """关闭数据库连接。"""
        try:
            self.conn.close()
        except Exception:
            pass

    def get_depth(self, doi: str) -> int:
        """获取指定 DOI 的滚雪球深度。"""
        try:
            cursor = self.conn.execute("SELECT depth FROM papers WHERE doi = ?", (doi,))
            row = cursor.fetchone()
            return row[0] if row else 0
        except Exception:
            return 0


# ╔══════════════════════════════════════════════════════════════════╗
# ║              模块 2：元数据获取  MetadataFetcher                  ║
# ╚══════════════════════════════════════════════════════════════════╝

class MetadataFetcher:
    """
    通过学术 API 获取论文元数据（标题、摘要）及其参考文献的 DOI 列表。
    主 API：Semantic Scholar
    备用 API：Crossref
    使用 tenacity 重试机制应对网络抖动。
    """

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "PaperHarvester/1.0 (Academic Research Tool; mailto:research@example.com)"
        })

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=8),
        retry=retry_if_exception(_is_retryable_error),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    def _request_semantic_scholar(self, doi: str) -> dict:
        """从 Semantic Scholar API 请求论文详情。"""
        url = f"{SEMANTIC_SCHOLAR_API}/paper/DOI:{doi}"
        params = {"fields": "title,abstract,references,references.externalIds"}
        try:
            resp = self.session.get(url, params=params, timeout=HTTP_TIMEOUT)
            if resp.status_code == 404:
                raise MetadataNotFoundError(f"Semantic Scholar 404: {doi}")
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            if isinstance(e, requests.exceptions.HTTPError) and e.response.status_code == 404:
                raise MetadataNotFoundError(f"Semantic Scholar 404: {doi}")
            raise MetadataTransientError(f"Semantic Scholar 网络错误: {e}")

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=8),
        retry=retry_if_exception(_is_retryable_error),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    def _request_crossref(self, doi: str) -> dict:
        """从 Crossref API 请求论文详情。"""
        url = f"{CROSSREF_API}/{doi}"
        try:
            resp = self.session.get(url, timeout=HTTP_TIMEOUT)
            if resp.status_code == 404:
                raise MetadataNotFoundError(f"Crossref 404: {doi}")
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            if isinstance(e, requests.exceptions.HTTPError) and e.response.status_code == 404:
                raise MetadataNotFoundError(f"Crossref 404: {doi}")
            raise MetadataTransientError(f"Crossref 网络错误: {e}")

    def _parse_semantic_scholar(self, data: dict) -> Tuple[str, str, List[str]]:
        """解析 Semantic Scholar 返回的数据。"""
        title = data.get("title", "") or ""
        abstract = data.get("abstract", "") or ""
        ref_dois = []
        references = data.get("references") or []
        for ref in references:
            ext_ids = ref.get("externalIds") or {}
            ref_doi = ext_ids.get("DOI")
            if ref_doi:
                ref_dois.append(ref_doi)
        return title, abstract, ref_dois

    def _parse_crossref(self, data: dict) -> Tuple[str, str, List[str]]:
        """解析 Crossref 返回的数据。"""
        message = data.get("message", {})
        # 标题可能是列表
        title_list = message.get("title", [])
        title = title_list[0] if title_list else ""
        abstract = message.get("abstract", "") or ""
        # 清理 abstract 中可能存在的 HTML 标签
        abstract = re.sub(r"<[^>]+>", "", abstract)
        ref_dois = []
        references = message.get("reference") or []
        for ref in references:
            ref_doi = ref.get("DOI")
            if ref_doi:
                ref_dois.append(ref_doi)
        return title, abstract, ref_dois

    def fetch(self, doi: str) -> Tuple[Optional[str], Optional[str], List[str], bool]:
        """
        获取指定 DOI 的元数据。
        返回: (title, abstract, ref_dois_list, is_not_found)
        """
        title = None
        abstract = None
        ref_dois = []
        is_not_found = False
        potential_not_found_sources = 0
        
        # 尝试 Semantic Scholar
        try:
            logger.info(f"[cyan]📡 Semantic Scholar 获取元数据: {doi}[/]")
            data = self._request_semantic_scholar(doi)
            title, abstract, ref_dois = self._parse_semantic_scholar(data)
            time.sleep(API_CALL_INTERVAL)
        except MetadataNotFoundError:
            logger.warning(f"[yellow]  ⚠ Semantic Scholar 明确提示 404[/]")
            potential_not_found_sources += 1
        except MetadataTransientError as e:
            logger.warning(f"[yellow]  ⚠ Semantic Scholar 临时请求失败: {e}[/]")
        except Exception as e:
            logger.warning(f"[yellow]  ⚠ Semantic Scholar 未知错误: {type(e).__name__}: {e}[/]")

        # 如果数据完美（有标题、有摘要、且有引用文献），直接返回
        if title and abstract and ref_dois:
            title_str = str(title) if title else ""
            logger.info(
                f"[green]  ✓ 完美获取 | 标题: {title_str[:50]}... | 引用: {len(ref_dois)} 篇[/]"
            )
            return title, abstract, ref_dois, is_not_found

        # 如果 Semantic Scholar 缺少核心数据（无摘要，或 0 引用），尝试 Crossref 互补
        if not title or not abstract or not ref_dois:
            if title:
                logger.info(f"[cyan]📡 Semantic Scholar 数据不全(引用={len(ref_dois)} 篇)，尝试 Crossref 互补...[/]")
            else:
                logger.info(f"[cyan]📡 Crossref 获取元数据 (首选失败后的备用): {doi}[/]")
                
            try:
                data_cr = self._request_crossref(doi)
                title_cr, abstract_cr, ref_dois_cr = self._parse_crossref(data_cr)
                time.sleep(API_CALL_INTERVAL)
                
                # 合并数据（保留已有数据，补充缺失数据）
                title = title or title_cr
                abstract = abstract or abstract_cr
                
                # 引用文献：取数量最多的那个，或者直接合并去重
                if len(ref_dois_cr) > len(ref_dois):
                    ref_dois = ref_dois_cr
                    
            except MetadataNotFoundError:
                logger.warning(f"[yellow]  ⚠ Crossref 明确提示 404[/]")
                potential_not_found_sources += 1
            except MetadataTransientError as e:
                logger.warning(f"[yellow]  ⚠ Crossref 临时请求失败: {e}[/]")
            except Exception as e:
                logger.warning(f"[yellow]  ⚠ Crossref 未知错误: {type(e).__name__}: {e}[/]")

        if title:
            title_str = str(title) if title else ""
            logger.info(
                f"[green]  ✓ 最终获取 | 标题: {title_str[:50]}... | 引用: {len(ref_dois)} 篇[/]"
            )
        else:
            if potential_not_found_sources >= 2:
                logger.error(f"[red]  ✗ 无法获取 {doi} 的元数据 (两个源均返回 404)[/]")
                is_not_found = True
            else:
                logger.error(f"[red]  ✗ 无法获取 {doi} 的元数据 (网络故障，保持 pending)[/]")

        return title, abstract, ref_dois, is_not_found


# ╔══════════════════════════════════════════════════════════════════╗
# ║              模块 3：相关性评估  RelevanceEvaluator               ║
# ╚══════════════════════════════════════════════════════════════════╗

class RelevanceEvaluator:
    """
    使用 DeepSeek API 评估论文与课题的相关性。
    输出 JSON: {"score": 1|2|3, "reason": "..."}
      1 = 核心相关（必须下载）
      2 = 部分相关（值得下载）
      3 = 不相关（丢弃）
    """

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })

    def _build_prompt(self, title: str, abstract: str, topic: str) -> str:
        """构造给 DeepSeek 的精密评估 Prompt。"""
        return f"""你是一位学术文献相关性评估专家。请根据以下信息，判断该论文与目标研究课题的相关程度。

【目标研究课题】
{topic}

【待评估论文】
标题: {title}
摘要: {abstract if abstract else '（摘要缺失，请仅根据标题判断）'}

【评分标准】
- 1 分 (Highly Relevant): 论文直接研究目标课题的核心问题，包含关键方法或实验数据。
- 2 分 (Somewhat Relevant): 论文涉及目标课题的相关技术、材料或背景知识，可提供参考价值。
- 3 分 (Irrelevant): 论文与目标课题无关或仅有极其微弱的间接联系。

【输出要求】
你必须且只能输出一个 JSON 对象，不要输出任何其他文字、解释或 Markdown 格式。
格式如下:
{{"score": <1或2或3>, "reason": "<简短理由，不超过50字>"}}"""

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=8),
        retry=retry_if_exception(_is_retryable_error),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    def _call_deepseek(self, prompt: str) -> str:
        """调用 DeepSeek API 并返回模型的原始文本回复。"""
        payload = {
            "model": DEEPSEEK_MODEL,
            "messages": [
                {"role": "system", "content": "你是一位严谨的学术文献评估助手，只输出 JSON。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 200,
        }
        resp = self.session.post(DEEPSEEK_API_URL, json=payload, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        # 提取模型回复
        content = data["choices"][0]["message"]["content"].strip()
        return content

    def _parse_response(self, raw: str) -> Tuple[int, str]:
        """
        解析模型回复的 JSON。
        容错：如果 JSON 解析失败，默认 score=2（保守策略，保证不丢文献）。
        """
        try:
            # 模型有时会在 JSON 外面包一层 ```json ... ```，先清理
            cleaned: str = str(raw).strip()
            # 去除可能的 Markdown 代码块标记
            if cleaned.startswith("```"):
                # 找到第一个换行后的内容
                first_newline = cleaned.find("\n")
                last_backtick = cleaned.rfind("```")
                if last_backtick > first_newline and first_newline != -1:
                    cleaned = cleaned[first_newline + 1 : last_backtick].strip()
                else:
                    cleaned = cleaned[first_newline + 1 :].strip()

            result = json.loads(cleaned)
            score = int(result.get("score", 2))
            reason = str(result.get("reason", "无"))

            # 确保 score 在有效范围内
            if score not in (1, 2, 3):
                logger.warning(f"[yellow]  ⚠ 评分 {score} 超出范围，修正为 2[/]")
                score = 2

            return score, reason

        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
            raw_str = str(raw) if raw else ""
            logger.warning(
                f"[yellow]  ⚠ DeepSeek 回复解析失败 ({type(e).__name__}), 默认 score=2。原始回复: {raw_str[:100]}[/]"
            )
            return 2, f"解析失败，默认评分 (原始: {raw_str[:80]})"

    def evaluate(self, title: str, abstract: str, topic: str) -> Tuple[int, str]:
        """
        评估论文相关性。
        返回: (score, reason)
        如果 API 调用完全失败，也返回 (2, reason) 以保证流程不中断。
        """
        if not title and not abstract:
            return 2, "标题和摘要均缺失，无法评估，默认部分相关"

        prompt = self._build_prompt(title, abstract, topic)

        try:
            logger.info(f"[magenta]🤖 DeepSeek 评估相关性...[/]")
            raw_response = self._call_deepseek(prompt)
            score, reason = self._parse_response(raw_response)
            score_labels = {1: "🔴 核心相关", 2: "🟡 部分相关", 3: "⚪ 不相关"}
            label = score_labels.get(score, "未知")
            logger.info(f"[magenta]  → {label} (score={score}): {reason}[/]")
            time.sleep(API_CALL_INTERVAL)
            return score, reason

        except Exception as e:
            logger.error(f"[red]  ✗ DeepSeek API 调用失败: {type(e).__name__}: {e}[/]")
            return 2, f"API 调用失败 ({type(e).__name__})，默认部分相关"


# ╔══════════════════════════════════════════════════════════════════╗
# ║                模块 4：下载器  PaperDownloader                    ║
# ╚══════════════════════════════════════════════════════════════════╝

class PaperDownloader:
    """
    通过 subprocess 调用 download_single.py 下载论文 PDF。
    根据子进程 exit code 判断成功/失败。
    """

    def __init__(self, script_path: Path = BASE_DIR / "src" / "download_single.py", output_dir: Path = OUTPUT_DIR):
        self.script_path = script_path
        self.output_dir = output_dir
        if not self.script_path.exists():
            logger.error(f"[red]✗ 下载脚本不存在: {self.script_path}[/]")
            logger.error(f"[red]  请确保 download_single.py 在 {BASE_DIR / 'src'} 目录下[/]")

    def download(self, doi: str, output_dir: Optional[Path] = None) -> int:
        """
        调用下载脚本下载指定 DOI 的论文。
        返回码 (对应 download_single.py 的 exit code):
        0 - 下载成功
        1 - 临时失败 (网络等)
        2 - 明确找不到文献 (Not Found)
        """
        if not self.script_path.exists():
            logger.error(f"[red]  ✗ 下载脚本缺失，跳过下载[/]")
            return False

        logger.info(f"[blue]📥 调用下载脚本: {doi}[/]")

        try:
            # 传递输出目录给下载脚本
            target_dir: Path = output_dir if output_dir else self.output_dir
            command = [sys.executable, str(self.script_path), doi, "--output-dir", str(target_dir)]
            result = subprocess.run(
                command,
                capture_output=True,
                timeout=DOWNLOAD_SUBPROCESS_TIMEOUT,
                cwd=str(BASE_DIR),  # 在项目根目录执行
            )

            # 解码子进程输出（兼容 Windows GBK 和 UTF-8）
            stdout_text = result.stdout.decode("utf-8", errors="replace") if result.stdout else ""
            stderr_text = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""

            # 输出子进程的标准输出用于调试
            if stdout_text.strip():
                for line in stdout_text.strip().split("\n"):
                    logger.info(f"[dim]  [下载] {line}[/]")
            if stderr_text.strip():
                for line in stderr_text.strip().split("\n"):
                    logger.warning(f"[dim]  [下载-err] {line}[/]")

            if result.returncode == 0:
                logger.info(f"[green]  ✓ 下载完成: {doi}[/]")
            elif result.returncode == 2:
                logger.warning(f"[yellow]  ✗ 文献在 Sci-Hub 中不存在 (404): {doi}[/]")
            else:
                logger.warning(f"[red]  ✗ 下载临时失败 (exit code={result.returncode}): {doi}[/]")
            
            return result.returncode

        except subprocess.TimeoutExpired:
            logger.error(f"[red]  ✗ 下载超时 ({DOWNLOAD_SUBPROCESS_TIMEOUT}s): {doi}[/]")
            return 1  # 临时失败
        except Exception as e:
            logger.error(f"[red]  ✗ 下载异常: {type(e).__name__}: {e}[/]")
            return 1  # 临时失败


# ╔══════════════════════════════════════════════════════════════════╗
# ║                      主 循 环  main()                            ║
# ╚══════════════════════════════════════════════════════════════════╝

def print_status_table(db: PaperDatabase):
    """使用 rich 打印当前进度表格。"""
    stats = db.count_by_status()
    table = Table(title="📊 PaperHarvester 当前进度", show_header=True)
    table.add_column("状态", style="bold")
    table.add_column("数量", justify="right")
    status_icons = {
        "pending": "⏳ 待处理",
        "evaluated": "🔍 已评估(不相关)",
        "downloaded": "✅ 已下载",
        "failed": "❌ 下载失败",
    }
    for status_key in ["pending", "evaluated", "downloaded", "failed"]:
        count = stats.get(status_key, 0)
        label = status_icons.get(status_key, status_key)
        table.add_row(label, str(count))
    table.add_row("[bold]总计[/]", f"[bold]{db.total_count()}[/]")
    console.print(table)

def extract_dois_from_pdf(pdf_path: Path) -> List[str]:
    """从本地 PDF 文件内容中正则匹配提取 DOI。"""
    dois = set()
    try:
        with open(pdf_path, 'rb') as f:
            content = f.read()
            # 常见 DOI 正则表达式
            # 匹配 10. 开头，且不再贪婪捕获括号和标点
            matches = re.findall(br'(10\.\d{4,9}/[^\s"\'<>]+)', content)
            for m in matches:
                try:
                    doi_str = m.decode('utf-8', errors='ignore')
                    # 严格清理尾部常见的无意义字符，特别是导致 bug 的右括号或点号
                    doi_str = re.sub(r'[.,;:)\]}]+$', '', doi_str)
                    if len(doi_str) > 8 and '/' in doi_str:
                        dois.add(doi_str)
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"[yellow]无法读取 PDF {pdf_path.name}: {e}[/]")
    return list(dois)


def get_seed_dois() -> List[str]:
    """先从 todo 文件夹的 PDF 中提取 DOI，如果没有则使用默认 SEED_DOIS。"""
    seeds = set()
    if TODO_DIR.exists():
        pdf_files = list(TODO_DIR.glob("*.pdf"))
        if pdf_files:
            logger.info(f"[cyan]从 {TODO_DIR.name} 文件夹中找到 {len(pdf_files)} 个 PDF 文件，正在提取 DOI...[/]")
            for pdf in pdf_files:
                extracted = extract_dois_from_pdf(pdf)
                if extracted:
                    # 我们通常认为 PDF 文本中出现的最早的 DOI 极有可能是文章本身的 DOI，或者是相关的
                    # 为了种子，我们把出现的所有合法 DOI 都当作种子（或者只取第一个）
                    # 考虑到种子需要准确，我们直接全加入种子池让网络过滤。
                    seeds.update(extracted)
                    logger.info(f"[dim]  从 {pdf.name} 提取了 {len(extracted)} 个潜在 DOI[/]")
    
    if not seeds:
        logger.info("[yellow]未从 todo 文件夹提取到 DOI，使用默认种子...[/]")
        seeds.update(DEFAULT_SEED_DOIS)
        
    return list(seeds)


def main():
    """PaperHarvester 主入口。"""

    # 确保文件夹存在
    TODO_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    # ── 启动横幅 ──
    console.print(
        Panel(
            f"[bold white]课题:[/] [yellow]{TOPIC_DESCRIPTION}[/]\n"
            f"[bold white]目标下载:[/] [green]{TARGET_DOWNLOAD_COUNT} 篇[/]  |  [bold white]种子获取来源:[/] [cyan]{TODO_DIR.name}/[/]\n"
            f"[bold white]下载目录:[/] [cyan]{OUTPUT_DIR.name}/[/]",
            title="[bold blue]PaperHarvester — 自动化文献滚雪球检索系统[/]",
            expand=False,
        )
    )

    # ── 加载 API Key ──
    load_dotenv(ENV_PATH, override=True)
    # .env 文件可能直接是裸 key（没有变量名），也可能是 DEEPSEEK_API_KEY=xxx
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        # 尝试直接读取 .env 文件内容作为 key
        try:
            raw = ENV_PATH.read_text(encoding="utf-8").strip()
            # 如果文件内容看起来像一个 API key（以 sk- 开头）
            if raw.startswith("sk-"):
                api_key = raw
            else:
                # 尝试解析 KEY=VALUE 格式
                for line in raw.split("\n"):
                    line = line.strip()
                    if line and not line.startswith("#"):
                        if "=" in line:
                            _, val = line.split("=", 1)
                            api_key = val.strip().strip("'\"")
                        else:
                            api_key = line
                        break
        except Exception:
            pass

    if not api_key:
        logger.error("[red]✗ 未找到 DeepSeek API Key！请在 .env 中设置。[/]")
        sys.exit(1)
        
    api_key_str: str = str(api_key)
    logger.info(f"[green]✓ API Key 已加载 (sk-...{api_key_str[-6:]})[/]")

    # ── 初始化各模块 ──
    db = PaperDatabase()
    fetcher = MetadataFetcher()
    evaluator = RelevanceEvaluator(api_key_str)
    downloader = PaperDownloader(output_dir=OUTPUT_DIR) # Pass output_dir here

    # ── 步骤 1：插入种子 DOI ──
    seed_added_count: int = 0
    seed_dois = get_seed_dois() # Get DOIs dynamically
    for doi in seed_dois:
        if db.add_doi(doi):
            seed_added_count = int(seed_added_count) + 1
    if seed_added_count > 0:
        logger.info(f"[green]✓ 新增 {seed_added_count} 个种子 DOI 到数据库[/]")
    else:
        logger.info(f"[dim]种子 DOI 已在数据库中（断点续传模式）[/]")

    print_status_table(db)

    # ── 步骤 2：主循环 ──
    iteration = 0
    start_time = time.time()
    last_active_time = time.time()
    
    try:
        while True:
            # 2a. 检查已下载数量
            downloaded_count = db.count_downloaded()
            if downloaded_count >= TARGET_DOWNLOAD_COUNT:
                logger.info(f"[green]🎉 目标达成！已下载 {downloaded_count}/{TARGET_DOWNLOAD_COUNT} 篇相关文献。[/]")
                break

            # 2b. 检查全局超时
            elapsed = time.time() - start_time
            if elapsed > MAX_RUNTIME_SEC:
                logger.warning(f"[yellow]⏰ 已达到全局最大运行时间 ({MAX_RUNTIME_SEC}s)，程序将自动退出以防长时间卡死。[/]")
                break

            # 2c. 检查非活动超时
            inactive_duration = time.time() - last_active_time
            if inactive_duration > INACTIVITY_TIMEOUT_SEC:
                logger.warning(f"[yellow]⏳ 已超过 {INACTIVITY_TIMEOUT_SEC}s 未能成功下载新文献，可能陷入僵局，程序将自动退出。[/]")
                break

            iteration += 1

            # 2d. 从数据库获取下一个 pending DOI
            doi = db.get_next_pending()
            if doi is None:
                logger.info("[yellow]📭 队列为空，所有候选文献已处理完毕[/]")
                break

            console.rule(f"[bold]第 {iteration} 轮  |  已下载: {downloaded_count}/{TARGET_DOWNLOAD_COUNT}  |  运行: {int(elapsed)}s")
            logger.info(f"[bold white]📄 处理 DOI: {doi}[/]")

            # 2e. 获取元数据和 Abstract
            title, abstract, ref_dois, is_not_found = fetcher.fetch(doi)

            # 更新 DOI 的活跃时间（获取到元数据也算一种进度，但不如成功下载进度强）
            # 我们这里选择仅在成功下载时刷新 inactivity_timeout_sec，或者在获取到新元数据时也刷新
            # 考虑到“卡住”通常发生在下载环节，刷新一次元数据获取时间也是合理的
            if title:
                # 即使没下载，获取到元数据也证明程序在推进
                pass 

            # 如果明确找不到元数据，标记为 failed
            if is_not_found:
                db.update_paper(doi, status="failed")
                continue

            # 更新数据库中的标题和摘要
            if title:
                db.update_paper(doi, title=title, abstract=abstract)

            # 如果没拿到元数据且不是因为 404，维持 pending 状态并跳过
            if not title:
                logger.warning(f"[yellow]  ⚠ 无法获取元数据且非明确 404，跳过此 DOI 保持已更新时间（回退到队尾）[/]")
                db.update_paper(doi) # 触发 updated_at 更新，使其排到后面
                continue

            # 2f. 调用 DeepSeek 进行打分
            score, reason = evaluator.evaluate(title, abstract, TOPIC_DESCRIPTION)

            # 更新评分到数据库
            db.update_paper(doi, relevance_score=score)

            # 2g. 根据评分决定是否下载
            if score in (1, 2):
                # 相关（核心相关或部分相关），尝试下载
                score_dir_name: str = "core_papers" if score == 1 else "relevant_papers"
                score_output_dir: Path = OUTPUT_DIR / score_dir_name
                exit_code = downloader.download(doi, output_dir=score_output_dir)

                success = False
                if exit_code == 0:
                    db.update_paper(doi, status="downloaded")
                    success = True
                    last_active_time = time.time() # 核心：成功下载，刷新非活动计时器
                elif exit_code == 2:
                    db.update_paper(doi, status="failed")
                else:
                    # 临时失败，保持 pending 但更新 updated_at
                    logger.info(f"[yellow]  ⚠ 下载遇到临时困难，DOI {doi} 将移至队尾稍后重试[/]")
                    db.update_paper(doi) 
                    continue

                # ── 滚雪球逻辑 ──
                current_depth = db.get_depth(doi)
                next_depth = current_depth + 1

                # 只有未超过最大深度才继续滚雪球
                if SNOWBALL_ENABLED and next_depth <= SNOWBALL_MAX_DEPTH:
                    # 来源 1：API 元数据中的引用文献
                    if ref_dois:
                        newly_added = db.add_dois_batch(ref_dois, depth=next_depth)
                        logger.info(
                            f"[cyan]  📚 [滚雪球 L{next_depth}] API引用文献: 共 {len(ref_dois)} 篇, 新增入库 {newly_added} 篇[/]"
                        )

                    # 来源 2：从下载的 PDF 文件中提取 DOI
                    if SNOWBALL_FROM_PDF and success:
                        safe_name = doi.replace("/", "_")
                        safe_name = re.sub(r'[<>:"|?*\\]', "_", safe_name)
                        safe_name_str: str = safe_name
                        pdf_file = score_output_dir / f"{safe_name_str}.pdf"
                        if pdf_file.exists():
                            pdf_dois = extract_dois_from_pdf(pdf_file)
                            # 去掉自己的 DOI
                            pdf_dois = [d for d in pdf_dois if d != doi]
                            if pdf_dois:
                                pdf_added = db.add_dois_batch(pdf_dois, depth=next_depth)
                                logger.info(
                                    f"[cyan]  📚 [滚雪球 L{next_depth}] PDF提取引用: 共 {len(pdf_dois)} 篇, 新增入库 {pdf_added} 篇[/]"
                                )
                elif not SNOWBALL_ENABLED:
                    logger.info(f"[dim]  ℹ️ 滚雪球已关闭 (配置 snowball.enabled=false)[/]")
                else:
                    logger.info(f"[dim]  ℹ️ 已达最大滚雪球深度 {SNOWBALL_MAX_DEPTH}，不再继续提取引用[/]")
            else:
                # 不相关（score=3），标记为 evaluated，不下载、不提取引用
                db.update_paper(doi, status="evaluated")
                logger.info(f"[dim]  ⏭ 不相关，跳过下载和引用提取[/]")

            # 每 10 轮打印一次详细进度
            if iteration % 10 == 0:
                print_status_table(db)

    except KeyboardInterrupt:
        console.print("\n")
        logger.info("[yellow]⚡ 用户中断 (Ctrl+C)，正在优雅退出...[/]")

    # ── 结束：打印最终统计 ──
    console.print()
    print_status_table(db)
    downloaded = db.count_downloaded()
    if downloaded >= TARGET_DOWNLOAD_COUNT:
        console.print(Panel.fit(
            f"[bold green]🎉 目标达成！已下载 {downloaded}/{TARGET_DOWNLOAD_COUNT} 篇相关文献。[/]",
            border_style="green",
        ))
    else:
        console.print(Panel.fit(
            f"[bold yellow]📋 已下载 {downloaded}/{TARGET_DOWNLOAD_COUNT} 篇。"
            f"{'队列为空。' if db.get_next_pending() is None else '程序中断，可重新运行继续。'}[/]",
            border_style="yellow",
        ))

    db.close()
    logger.info("[dim]数据库连接已关闭，程序退出。[/]")


if __name__ == "__main__":
    main()
