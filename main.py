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
import hashlib
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

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

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
    """从 config.json 加载配置，支持多主题格式，兼容旧单主题格式。"""
    defaults: Dict[str, Any] = {
        "storage_path": "",
        "target_download_count": 50,
        "topics": [{"name": "默认主题", "description": "火箭发动机推力室一体化设计"}],
        "paths": {
            "download_script": "src/download_single.py",
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
            # 向后兼容：旧单主题格式自动转换
            if "topic_description" in user_cfg and "topics" not in user_cfg:
                user_cfg["topics"] = [{
                    "name": "默认主题",
                    "description": user_cfg.pop("topic_description")
                }]
            # 深度合并
            for k, v in user_cfg.items():
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
TARGET_DOWNLOAD_COUNT: int = CFG["target_download_count"]
TOPICS: List[Dict[str, str]] = CFG["topics"]

# ── 存储路径计算 ──
_storage_path_raw: str = CFG.get("storage_path", "")
if _storage_path_raw:
    STORAGE_ROOT = Path(_storage_path_raw)
else:
    STORAGE_ROOT = BASE_DIR  # 未指定时使用项目目录

TODO_DIR       = STORAGE_ROOT / "todo"
DB_PATH        = STORAGE_ROOT / "papers.db"
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
MAX_METADATA_RETRIES        = CFG.get("max_metadata_retries", 3)

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
        """创建 papers 表（如不存在），并自动迁移旧表结构。"""
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS papers (
                doi         TEXT PRIMARY KEY,
                title       TEXT DEFAULT '',
                abstract    TEXT DEFAULT '',
                relevance_score INTEGER DEFAULT 0,
                best_topic  TEXT DEFAULT '',
                status      TEXT DEFAULT 'pending',
                depth       INTEGER DEFAULT 0,
                parent_score INTEGER DEFAULT 5,
                retry_count INTEGER DEFAULT 0,
                added_at    TEXT DEFAULT '',
                updated_at  TEXT DEFAULT ''
            )
        """)
        # 新增：已处理种子文件跟踪表
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_seeds (
                file_hash    TEXT PRIMARY KEY,
                filename     TEXT NOT NULL,
                file_size    INTEGER DEFAULT 0,
                best_topic   TEXT DEFAULT '',
                score        INTEGER DEFAULT 0,
                processed_at TEXT DEFAULT '',
                doi_count    INTEGER DEFAULT 0
            )
        """)
        self.conn.commit()
        # 自动迁移：如果旧表缺少列，自动添加
        for col, typedef in [("parent_score", "INTEGER DEFAULT 5"), ("best_topic", "TEXT DEFAULT ''"), ("retry_count", "INTEGER DEFAULT 0")]:
            try:
                self.conn.execute(f"SELECT {col} FROM papers LIMIT 1")
            except sqlite3.OperationalError:
                self.conn.execute(f"ALTER TABLE papers ADD COLUMN {col} {typedef}")
                self.conn.commit()
                logger.info(f"[dim]数据库迁移：已添加 {col} 列[/]")

    def add_doi(self, doi: str, depth: int = 0, parent_score: int = 5) -> bool:
        """
        添加新 DOI 到数据库（状态为 pending）。
        parent_score: 父文献的相关性评分，用于队列优先级排序。
        如果 DOI 已存在则静默忽略，返回 False。
        """
        doi = doi.strip()
        if not doi:
            return False
        now = datetime.now().isoformat()
        try:
            self.conn.execute(
                "INSERT INTO papers (doi, status, depth, parent_score, added_at, updated_at) VALUES (?, 'pending', ?, ?, ?, ?)",
                (doi, depth, parent_score, now, now),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            # DOI 已存在，静默忽略
            return False

    def add_dois_batch(self, dois: List[str], depth: int = 0, parent_score: int = 5) -> int:
        """
        批量添加 DOI 列表，返回实际新增数量。
        parent_score: 父文献的相关性评分，用于队列优先级排序。
        """
        added_count: int = 0
        now = datetime.now().isoformat()
        for doi in dois:
            doi = doi.strip()
            if not doi:
                continue
            try:
                self.conn.execute(
                    "INSERT INTO papers (doi, status, depth, parent_score, added_at, updated_at) VALUES (?, 'pending', ?, ?, ?, ?)",
                    (doi, depth, parent_score, now, now),
                )
                added_count = int(added_count) + 1
            except sqlite3.IntegrityError:
                continue
        self.conn.commit()
        return added_count

    def get_next_pending(self) -> Optional[str]:
        """
        获取下一个状态为 pending 的 DOI。
        优先级排序：parent_score DESC (高相关分支优先) → depth ASC → updated_at ASC
        返回 DOI 字符串或 None（队列为空时）。
        """
        cursor = self.conn.execute(
            "SELECT doi FROM papers WHERE status = 'pending' "
            "ORDER BY parent_score DESC, depth ASC, updated_at ASC LIMIT 1"
        )
        row = cursor.fetchone()
        return row[0] if row else None

    def update_paper(
        self,
        doi: str,
        title: Optional[str] = None,
        abstract: Optional[str] = None,
        relevance_score: Optional[int] = None,
        best_topic: Optional[str] = None,
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
        if best_topic is not None:
            fields.append("best_topic = ?")
            values.append(best_topic)
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

    def increment_retry(self, doi: str) -> int:
        """增加指定 DOI 的重试计数并返回新值。"""
        now = datetime.now().isoformat()
        self.conn.execute(
            "UPDATE papers SET retry_count = retry_count + 1, updated_at = ? WHERE doi = ?",
            (now, doi),
        )
        self.conn.commit()
        cursor = self.conn.execute("SELECT retry_count FROM papers WHERE doi = ?", (doi,))
        row = cursor.fetchone()
        return row[0] if row else 0

    # ── 种子文件跟踪方法 ──

    @staticmethod
    def compute_seed_hash(pdf_path: Path) -> str:
        """计算种子 PDF 的指纹：sha256(文件名|大小|修改时间)。"""
        stat = pdf_path.stat()
        fingerprint = f"{pdf_path.name}|{stat.st_size}|{stat.st_mtime}"
        return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()

    def is_seed_processed(self, file_hash: str) -> bool:
        """检查指定指纹的种子文件是否已处理。"""
        cursor = self.conn.execute(
            "SELECT 1 FROM processed_seeds WHERE file_hash = ? LIMIT 1", (file_hash,)
        )
        return cursor.fetchone() is not None

    def mark_seed_processed(
        self,
        file_hash: str,
        filename: str,
        file_size: int,
        best_topic: str = "",
        score: int = 0,
        doi_count: int = 0,
    ):
        """标记一个种子文件为已处理。"""
        now = datetime.now().isoformat()
        try:
            self.conn.execute(
                "INSERT OR REPLACE INTO processed_seeds "
                "(file_hash, filename, file_size, best_topic, score, processed_at, doi_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (file_hash, filename, file_size, best_topic, score, now, doi_count),
            )
            self.conn.commit()
        except Exception as e:
            logger.warning(f"[yellow]标记种子文件失败: {e}[/]")

    def count_processed_seeds(self) -> int:
        """返回已处理的种子文件总数。"""
        cursor = self.conn.execute("SELECT COUNT(*) FROM processed_seeds")
        return cursor.fetchone()[0]


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
        """构造给 DeepSeek 的精密评估 Prompt（10 级评分）。"""
        return f"""你是一位学术文献相关性评估专家。请根据以下信息，判断该论文与目标研究课题的相关程度。

【目标研究课题】
{topic}

【待评估论文】
标题: {title}
摘要: {abstract if abstract else '（摘要缺失，请仅根据标题判断）'}

【评分标准 (1-10)】
- 9~10 分: 核心相关，论文直接研究目标课题的核心问题（如关键设计方法、实验验证、性能优化）。
- 7~8 分: 高度相关，论文涉及目标课题的重要子领域或关键支撑技术。
- 5~6 分: 中等相关，论文提供了与课题相关的基础理论、背景知识或通用方法。
- 3~4 分: 边缘相关，论文仅有间接的技术或领域联系，参考价值有限。
- 1~2 分: 不相关，论文与目标课题无实质性联系。

【输出要求】
你必须且只能输出一个 JSON 对象，不要输出任何其他文字、解释或 Markdown 格式。
格式如下:
{{"score": <1到10的整数>, "reason": "<简短理由，不超过50字>"}}"""

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
            "max_tokens": 500,
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
            score = int(result.get("score", 5))
            reason = str(result.get("reason", "无"))

            # 确保 score 在有效范围 1-10 内
            if score < 1 or score > 10:
                logger.warning(f"[yellow]  ⚠ 评分 {score} 超出 1-10 范围，修正为 5[/]")
                score = max(1, min(10, score))

            return score, reason

        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
            raw_str = str(raw) if raw else ""
            logger.warning(
                f"[yellow]  ⚠ DeepSeek 回复解析失败 ({type(e).__name__}), 默认 score=5。原始回复: {raw_str[:100]}[/]"
            )
            return 5, f"解析失败，默认评分 (原始: {raw_str[:80]})"

    def evaluate(self, title: str, abstract: str, topic: str) -> Tuple[int, str]:
        """
        评估论文相关性（10 级评分）。
        返回: (score, reason)  score ∈ [1, 10]
        如果 API 调用完全失败，返回 (5, reason) 保守处理。
        """
        if not title and not abstract:
            return 5, "标题和摘要均缺失，无法评估，默认中等相关"

        prompt = self._build_prompt(title, abstract, topic)

        try:
            logger.info(f"[magenta]🤖 DeepSeek 评估相关性...[/]")
            raw_response = self._call_deepseek(prompt)
            score, reason = self._parse_response(raw_response)
            # 10 级标签
            if score >= 9:
                label = "🔴 核心相关"
            elif score >= 7:
                label = "🟠 高度相关"
            elif score >= 5:
                label = "🟡 中等相关"
            elif score >= 3:
                label = "🔵 边缘相关"
            else:
                label = "⚪ 不相关"
            logger.info(f"[magenta]  → {label} (score={score}/10): {reason}[/]")
            time.sleep(API_CALL_INTERVAL)
            return score, reason

        except Exception as e:
            logger.error(f"[red]  ✗ DeepSeek API 调用失败: {type(e).__name__}: {e}[/]")
            return 5, f"API 调用失败 ({type(e).__name__})，默认中等相关"

    def _build_multi_prompt(self, title: str, abstract: str, topics: List[Dict[str, str]]) -> str:
        """构造多主题同时评估的 Prompt。"""
        topics_text = "\n".join(
            f"  {i+1}. 【{t['name']}】: {t['description']}"
            for i, t in enumerate(topics)
        )
        return f"""你是一位学术文献相关性评估专家。请根据以下信息，判断该论文与每个研究方向的契合程度。

【研究方向列表】
{topics_text}

【待评估论文】
标题: {title}
摘要: {abstract if abstract else '（摘要缺失，请仅根据标题判断）'}

【评分标准 (1-10)】
- 9~10 分: 核心相关，论文直接研究该方向的核心问题。
- 7~8 分: 高度相关，论文涉及该方向的重要子领域或关键支撑技术。
- 5~6 分: 中等相关，论文提供了与该方向相关的基础理论或通用方法。
- 3~4 分: 边缘相关，论文仅有间接的技术联系。
- 1~2 分: 不相关。

【输出要求】
对每个方向分别打分。你必须且只能输出一个 JSON 对象，不要输出任何其他文字。
格式如下:
{{"scores": [{{"topic": "<方向名称>", "score": <1到10的整数>, "reason": "<简短理由，不超30字>"}}]}}"""

    def _parse_multi_response(self, raw: str, topics: List[Dict[str, str]]) -> List[Dict[str, Any]]:
        """
        解析多主题评估结果。
        返回: [{"topic": "xxx", "score": N, "reason": "..."}, ...]
        """
        try:
            cleaned: str = str(raw).strip()
            if cleaned.startswith("```"):
                first_newline = cleaned.find("\n")
                last_backtick = cleaned.rfind("```")
                if last_backtick > first_newline and first_newline != -1:
                    cleaned = cleaned[first_newline + 1 : last_backtick].strip()
                else:
                    cleaned = cleaned[first_newline + 1 :].strip()

            result = json.loads(cleaned)
            scores_list = result.get("scores", [])

            # 校验并修正
            parsed: List[Dict[str, Any]] = []
            for item in scores_list:
                score = max(1, min(10, int(item.get("score", 5))))
                parsed.append({
                    "topic": str(item.get("topic", "")),
                    "score": score,
                    "reason": str(item.get("reason", "无")),
                })
            return parsed

        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
            raw_str = str(raw) if raw else ""
            logger.warning(
                f"[yellow]  ⚠ 多主题评估回复解析失败 ({type(e).__name__}), 使用默认评分。原始: {raw_str[:120]}[/]"
            )
            # 回退：给所有主题默认 5 分
            return [{"topic": t["name"], "score": 5, "reason": "解析失败，默认评分"} for t in topics]

    def evaluate_multi(self, title: str, abstract: str, topics: List[Dict[str, str]]) -> Tuple[int, str, str]:
        """
        多主题评估：一次 API 调用评估论文与所有研究方向的契合度。
        返回: (best_score, best_topic_name, reason)
        """
        if not title and not abstract:
            return 5, topics[0]["name"], "标题和摘要均缺失，默认归入第一个方向"

        prompt = self._build_multi_prompt(title, abstract, topics)

        try:
            logger.info(f"[magenta]🤖 DeepSeek 多主题评估 ({len(topics)} 个方向)...[/]")
            raw_response = self._call_deepseek(prompt)
            scores = self._parse_multi_response(raw_response, topics)

            # 找最高分的 topic
            best = max(scores, key=lambda x: x["score"]) if scores else {"topic": topics[0]["name"], "score": 5, "reason": "无评分结果"}

            # 打印所有方向的评分
            for s in scores:
                sc = s["score"]
                if sc >= 7:
                    color = "green"
                elif sc >= 5:
                    color = "yellow"
                else:
                    color = "dim"
                marker = " ★" if s["topic"] == best["topic"] else ""
                logger.info(f"[{color}]    {s['topic']}: {sc}/10 — {s['reason']}{marker}[/]")

            best_score = int(best["score"])
            if best_score >= 9:
                label = "🔴 核心相关"
            elif best_score >= 7:
                label = "🟠 高度相关"
            elif best_score >= 5:
                label = "🟡 中等相关"
            elif best_score >= 3:
                label = "🔵 边缘相关"
            else:
                label = "⚪ 不相关"
            logger.info(f"[magenta]  → 最佳归属: 【{best['topic']}】 {label} (score={best_score}/10)[/]")
            time.sleep(API_CALL_INTERVAL)
            return best_score, str(best["topic"]), str(best["reason"])

        except Exception as e:
            logger.error(f"[red]  ✗ DeepSeek 多主题评估失败: {type(e).__name__}: {e}[/]")
            return 5, topics[0]["name"], f"API 失败，默认归入 {topics[0]['name']}"


# ╔══════════════════════════════════════════════════════════════════╗
# ║                模块 4：下载器  PaperDownloader                    ║
# ╚══════════════════════════════════════════════════════════════════╝

class PaperDownloader:
    """
    通过 subprocess 调用 download_single.py 下载论文 PDF。
    根据子进程 exit code 判断成功/失败。
    """

    def __init__(self, script_path: Path = BASE_DIR / "src" / "download_single.py", output_dir: Path = STORAGE_ROOT):
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

# ── DOI 正则：清理并校验一个候选 DOI 字符串 ──
_DOI_RE = re.compile(r'(10\.\d{4,9}/[^\s"\',;<>\]\)}{]+)')

def _clean_doi(raw: str) -> Optional[str]:
    """清理候选 DOI 字符串，去掉尾部垃圾字符并做基本校验。"""
    doi = raw.strip()
    # 去掉 URL 参数（如 &domain=pdf）
    doi = re.sub(r'[&?].*$', '', doi)
    # 去掉尾部常见的非 DOI 字符
    doi = re.sub(r'[.,;:)\]}]+$', '', doi)
    # 去掉末尾可能粘上的 PDF 元数据噪声
    doi = re.sub(r'\\[a-zA-Z]+$', '', doi)
    if len(doi) > 8 and '/' in doi and not doi.endswith('/'):
        return doi
    return None


def _extract_full_text_from_pdf(pdf_path: Path) -> str:
    """
    使用 PyMuPDF (fitz) 从 PDF 中逐页提取文本。
    如果 PyMuPDF 不可用则返回空字符串。
    """
    if fitz is None:
        logger.warning("[yellow]PyMuPDF 未安装，无法提取 PDF 文本。请运行: pip install PyMuPDF[/]")
        return ""
    try:
        doc = fitz.open(str(pdf_path))
        pages_text: List[str] = []
        for page in doc:
            pages_text.append(page.get_text())
        doc.close()
        return "\n".join(pages_text)
    except Exception as e:
        logger.warning(f"[yellow]PyMuPDF 提取文本失败 ({pdf_path.name}): {e}[/]")
        return ""


def _find_references_section(full_text: str) -> str:
    """
    从全文中定位参考文献节。
    策略：反向搜索常见参考文献标题的最后一次出现位置。
    返回参考文献节的文本（从标题行之后开始）。
    """
    if not full_text:
        return ""

    # 多种可能的标题格式（含可选的编号前缀和换行）
    # 例如: "References", "REFERENCES", "8. References", "VI. REFERENCES", "参考文献"
    heading_patterns = [
        r'(?:^|\n)\s*(?:[0-9IVXLC]+\.?\s+)?References\s*(?:\n|$)',
        r'(?:^|\n)\s*(?:[0-9IVXLC]+\.?\s+)?REFERENCES\s*(?:\n|$)',
        r'(?:^|\n)\s*(?:[0-9IVXLC]+\.?\s+)?Bibliography\s*(?:\n|$)',
        r'(?:^|\n)\s*(?:[0-9IVXLC]+\.?\s+)?BIBLIOGRAPHY\s*(?:\n|$)',
        r'(?:^|\n)\s*参考文献\s*(?:\n|$)',
    ]

    last_pos = -1
    last_end = -1
    for pattern in heading_patterns:
        for match in re.finditer(pattern, full_text):
            if match.start() > last_pos:
                last_pos = match.start()
                last_end = match.end()

    if last_pos == -1:
        return ""

    ref_text = full_text[last_end:]

    # 尝试截断末尾可能的附录/致谢/作者简介等
    cutoff_patterns = [
        r'(?:^|\n)\s*(?:[0-9IVXLC]+\.?\s+)?(?:Appendix|APPENDIX|Acknowledgment|ACKNOWLEDGMENT|Acknowledgement|ACKNOWLEDGEMENT)',
        r'(?:^|\n)\s*(?:Author |About the Author)',
    ]
    earliest_cutoff = len(ref_text)
    for pattern in cutoff_patterns:
        match = re.search(pattern, ref_text)
        if match and match.start() < earliest_cutoff:
            earliest_cutoff = match.start()

    ref_text = ref_text[:earliest_cutoff].strip()
    return ref_text


def _parse_reference_entries(ref_text: str) -> List[str]:
    """
    将参考文献文本拆分为独立的引用条目列表。
    支持格式:
      - [1] Author, Title...      (方括号编号)
      - 1. Author, Title...       (数字+点)
      - 1Author, Title...         (上标数字，直接跟作者名)
      - {1} Author, Title...      (花括号编号)
    如果都不匹配，则按段落（双换行）拆分。
    """
    if not ref_text.strip():
        return []

    entries: List[str] = []

    # 策略 1: [N] 格式
    pattern_bracket = re.compile(r'(?:^|\n)\s*\[\d+\]')
    splits_bracket = list(pattern_bracket.finditer(ref_text))

    # 策略 2: N. 格式 (行首数字+点)
    pattern_dot = re.compile(r'(?:^|\n)\s*\d{1,3}\.\s')
    splits_dot = list(pattern_dot.finditer(ref_text))

    # 策略 3: N 上标格式 (行首数字直接跟大写字母/作者名)
    pattern_superscript = re.compile(r'(?:^|\n)\s*\d{1,3}[A-Z][a-z]')
    splits_superscript = list(pattern_superscript.finditer(ref_text))

    # 选择匹配数量最多的策略（≥3 条才认为有效）
    best_splits = []
    for candidate in [splits_bracket, splits_dot, splits_superscript]:
        if len(candidate) > len(best_splits):
            best_splits = candidate

    if len(best_splits) >= 3:
        for i, m in enumerate(best_splits):
            start = m.start()
            end = best_splits[i + 1].start() if i + 1 < len(best_splits) else len(ref_text)
            entry = ref_text[start:end].strip()
            entry = re.sub(r'^\s*\[?\d+\]?\.?\s*', '', entry)
            entry = ' '.join(entry.split())
            if len(entry) > 10:
                entries.append(entry)
    else:
        paragraphs = re.split(r'\n\s*\n', ref_text)
        for p in paragraphs:
            p = ' '.join(p.split()).strip()
            if len(p) > 20:
                entries.append(p)

    return entries


def _resolve_doi_via_crossref(query_text: str, session: requests.Session) -> Optional[str]:
    """
    使用 Crossref 的 query.bibliographic 查询，将一条引用文本解析为 DOI。
    返回最佳匹配的 DOI 或 None。
    """
    query = query_text[:200].strip()
    if len(query) < 15:
        return None

    url = f"{CROSSREF_API}"
    params = {
        "query.bibliographic": query,
        "rows": 1,
        "select": "DOI,title,score",
    }
    try:
        resp = session.get(url, params=params, timeout=15)
        if resp.status_code != 200:
            return None
        data = resp.json()
        items = data.get("message", {}).get("items", [])
        if not items:
            return None

        best = items[0]
        score = best.get("score", 0)
        if score < 30:
            return None

        return best.get("DOI")
    except Exception:
        return None


def _extract_dois_binary_fallback(pdf_path: Path) -> List[str]:
    """原始二进制正则兜底方案：直接扫描 PDF 字节流。"""
    dois = set()
    try:
        with open(pdf_path, 'rb') as f:
            content = f.read()
            matches = re.findall(br'(10\.\d{4,9}/[^\s"\'<>]+)', content)
            for m in matches:
                try:
                    doi_str = m.decode('utf-8', errors='ignore')
                    cleaned = _clean_doi(doi_str)
                    if cleaned:
                        dois.add(cleaned)
                except Exception:
                    pass
    except Exception:
        pass
    return list(dois)


def extract_dois_from_pdf(pdf_path: Path) -> List[str]:
    """
    从 PDF 文件中提取参考文献的 DOI 列表（多策略）。

    策略优先级：
      1. PyMuPDF 提取全文 → 定位 References 节 → 拆分引用条目
         a. 正则提取条目中已有的 DOI
         b. Crossref 查询将纯文本引用解析为 DOI
      2. 二进制正则扫描 PDF 字节流（兜底）
    """
    all_dois: set = set()

    # ── 策略 1: PyMuPDF 文本提取 ──
    full_text = _extract_full_text_from_pdf(pdf_path)
    if full_text:
        for m in _DOI_RE.finditer(full_text):
            cleaned = _clean_doi(m.group(1))
            if cleaned:
                all_dois.add(cleaned)

        text_doi_count = len(all_dois)
        logger.info(f"[cyan]  📄 PDF 全文 DOI 正则: 找到 {text_doi_count} 个[/]")

        ref_text = _find_references_section(full_text)
        if ref_text:
            entries = _parse_reference_entries(ref_text)
            logger.info(f"[cyan]  📚 References 节: 识别到 {len(entries)} 条引用[/]")

            if entries:
                crossref_session = requests.Session()
                crossref_session.headers.update({
                    "User-Agent": "PaperHarvester/1.0 (Academic Research Tool; mailto:research@example.com)"
                })
                crossref_resolved = 0
                crossref_failed = 0

                for i, entry in enumerate(entries):
                    doi_match = _DOI_RE.search(entry)
                    if doi_match:
                        cleaned = _clean_doi(doi_match.group(1))
                        if cleaned:
                            all_dois.add(cleaned)
                            continue

                    resolved = _resolve_doi_via_crossref(entry, crossref_session)
                    if resolved:
                        cleaned = _clean_doi(resolved)
                        if cleaned:
                            all_dois.add(cleaned)
                            crossref_resolved += 1
                    else:
                        crossref_failed += 1

                    time.sleep(0.3)

                    if (i + 1) % 10 == 0:
                        logger.info(
                            f"[dim]    Crossref 查询进度: {i + 1}/{len(entries)} "
                            f"(成功 {crossref_resolved}, 失败 {crossref_failed})[/]"
                        )

                logger.info(
                    f"[cyan]  🔗 Crossref 解析完成: 成功 {crossref_resolved}, "
                    f"失败 {crossref_failed}[/]"
                )
        else:
            logger.warning(f"[yellow]  ⚠ 未能在 PDF 中定位 References 节[/]")

    # ── 策略 2: 二进制正则兜底 ──
    binary_dois = _extract_dois_binary_fallback(pdf_path)
    pre_count = len(all_dois)
    all_dois.update(binary_dois)
    if len(all_dois) > pre_count:
        logger.info(f"[dim]  📎 二进制正则兜底新增 {len(all_dois) - pre_count} 个 DOI[/]")

    logger.info(f"[green]  ✓ PDF 总计提取 {len(all_dois)} 个唯一 DOI[/]")
    return list(all_dois)


def _extract_title_abstract_from_pdf(pdf_path: Path) -> Tuple[str, str]:
    """
    使用 PyMuPDF 从 PDF 前几页提取标题和摘要。
    标题：取第一页首几行非空文本（通常是最大字号）。
    摘要：从前 3 页中提取 Abstract 段落，若无则取前 500 字。
    """
    if fitz is None:
        return pdf_path.stem, ""
    try:
        doc = fitz.open(str(pdf_path))
        # 提取前 3 页文本
        pages_text = []
        for i, page in enumerate(doc):
            if i >= 3:
                break
            pages_text.append(page.get_text())
        doc.close()

        all_text = "\n".join(pages_text)
        if not all_text.strip():
            return pdf_path.stem, ""

        # 标题：取第一页前几行非空行
        first_page_lines = [l.strip() for l in pages_text[0].split("\n") if l.strip()]
        title = " ".join(first_page_lines[:3]) if first_page_lines else pdf_path.stem
        # 限制标题长度
        if len(title) > 200:
            title = title[:200]

        # 摘要：尝试找 Abstract 段落
        abstract = ""
        abstract_match = re.search(
            r'(?i)\babstract\b[.:\s]*(.+?)(?=\n\s*(?:keywords?|introduction|1[.\s]|I[.\s]))',
            all_text, re.DOTALL
        )
        if abstract_match:
            abstract = abstract_match.group(1).strip()
            abstract = ' '.join(abstract.split())  # 清理多余空白
        else:
            # 回退：取开头 500 字
            abstract = ' '.join(all_text[:1500].split())

        # 限制摘要长度
        if len(abstract) > 1000:
            abstract = abstract[:1000]

        return title, abstract
    except Exception as e:
        logger.warning(f"[yellow]从 PDF 提取标题/摘要失败 ({pdf_path.name}): {e}[/]")
        return pdf_path.stem, ""


def process_seed_papers(
    db: 'PaperDatabase',
    evaluator: 'RelevanceEvaluator',
    topics: List[Dict[str, str]],
) -> List[str]:
    """
    增量扫描 todo/ 文件夹中的 PDF，仅处理新增/变更的文件。
    通过 processed_seeds 表中的文件指纹跟踪已处理状态。

    流程：
      1. 遍历 todo/ 中所有 PDF，计算指纹
      2. 跳过指纹已在 processed_seeds 表中的文件
      3. 对新文件：提取标题 + 摘要 → 多主题评估 → 分类复制 → 提取 DOI
      4. 处理完成后标记到 processed_seeds 表
    
    返回: 所有新提取到的种子 DOI 列表
    """
    import shutil

    seeds: set = set()

    if not TODO_DIR.exists():
        logger.warning(f"[yellow]todo 文件夹不存在: {TODO_DIR}[/]")
        return []

    pdf_files = list(TODO_DIR.glob("*.pdf"))
    if not pdf_files:
        logger.info(f"[dim]todo 文件夹为空: {TODO_DIR}[/]")
        return []

    # ── 增量过滤：计算指纹，跳过已处理文件 ──
    new_pdfs: List[Tuple[Path, str]] = []  # (path, file_hash)
    skipped = 0
    for pdf in pdf_files:
        try:
            file_hash = PaperDatabase.compute_seed_hash(pdf)
            if db.is_seed_processed(file_hash):
                skipped += 1
            else:
                new_pdfs.append((pdf, file_hash))
        except Exception as e:
            logger.warning(f"[yellow]计算文件指纹失败 ({pdf.name}): {e}[/]")
            new_pdfs.append((pdf, ""))  # 无法计算指纹则视为新文件

    if skipped > 0:
        logger.info(f"[dim]📂 todo/ 共 {len(pdf_files)} 个 PDF，其中 {skipped} 个已处理过，跳过[/]")

    if not new_pdfs:
        logger.info(f"[dim]📂 todo/ 中没有新的种子文献需要处理[/]")
        return []

    logger.info(f"[cyan]📂 发现 {len(new_pdfs)} 个新输入文献，开始评估并分类...[/]")

    for idx, (pdf, file_hash) in enumerate(new_pdfs, 1):
        console.rule(f"[bold]输入文献 {idx}/{len(new_pdfs)}: {pdf.name}")

        # 1. 提取标题和摘要
        title, abstract = _extract_title_abstract_from_pdf(pdf)
        logger.info(f"[white]  📄 标题: {title[:80]}{'...' if len(title) > 80 else ''}[/]")
        if abstract:
            logger.info(f"[dim]  📝 摘要: {abstract[:100]}...[/]")

        # 2. 多主题评估
        score, best_topic, reason = evaluator.evaluate_multi(title, abstract, topics)

        # 3. 分类并复制
        if score >= 7:
            sub_dir = "core_papers"
        elif score >= 5:
            sub_dir = "relevant_papers"
        else:
            sub_dir = "relevant_papers"  # 输入文献至少保留

        dest_dir = STORAGE_ROOT / best_topic / sub_dir
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_file = dest_dir / pdf.name

        if not dest_file.exists():
            shutil.copy2(str(pdf), str(dest_file))
            logger.info(
                f"[green]  ✓ 已分类: {pdf.name} → {best_topic}/{sub_dir}/ (score={score}/10)[/]"
            )
        else:
            logger.info(
                f"[dim]  ℹ️ 已存在: {best_topic}/{sub_dir}/{pdf.name}，跳过复制[/]"
            )

        # 4. 提取 DOI 作为种子
        extracted_dois = extract_dois_from_pdf(pdf)
        doi_count = len(extracted_dois)
        if extracted_dois:
            seeds.update(extracted_dois)
            logger.info(f"[cyan]  🔗 从 {pdf.name} 提取了 {doi_count} 个种子 DOI[/]")

        # 5. 标记为已处理
        if file_hash:
            try:
                file_size = pdf.stat().st_size
            except Exception:
                file_size = 0
            db.mark_seed_processed(
                file_hash=file_hash,
                filename=pdf.name,
                file_size=file_size,
                best_topic=best_topic,
                score=score,
                doi_count=doi_count,
            )

    logger.info(f"[green]✓ 输入文献处理完成: {len(new_pdfs)} 篇新文献已分类, {len(seeds)} 个种子 DOI[/]")
    total_seeds = db.count_processed_seeds()
    logger.info(f"[dim]  累计已处理种子文件: {total_seeds} 个[/]")
    return list(seeds)


# ╔══════════════════════════════════════════════════════════════════╗
# ║              快捷方式创建 (Windows)                                ║
# ╚══════════════════════════════════════════════════════════════════╝

def _create_shortcut(target_path: Path, shortcut_path: Path):
    """在 Windows 上创建 .lnk 快捷方式（通过 PowerShell）。"""
    if sys.platform != "win32":
        logger.warning("[yellow]快捷方式仅在 Windows 上支持[/]")
        return
    try:
        ps_script = (
            f'$ws = New-Object -ComObject WScript.Shell; '
            f'$sc = $ws.CreateShortcut("{shortcut_path}"); '
            f'$sc.TargetPath = "{target_path}"; '
            f'$sc.Save()'
        )
        subprocess.run(
            ["powershell", "-Command", ps_script],
            capture_output=True, timeout=10,
        )
        logger.info(f"[green]  ✓ 已创建快捷方式: {shortcut_path.name} → {target_path}[/]")
    except Exception as e:
        logger.warning(f"[yellow]  ⚠ 创建快捷方式失败: {e}[/]")


# ╔══════════════════════════════════════════════════════════════════╗
# ║                      主 循 环  main()                            ║
# ╚══════════════════════════════════════════════════════════════════╝

def main():
    """PaperHarvester 主入口（多主题版）。"""

    # ── 创建存储目录结构 ──
    TODO_DIR.mkdir(parents=True, exist_ok=True)
    for topic in TOPICS:
        topic_name: str = topic["name"]
        (STORAGE_ROOT / topic_name / "core_papers").mkdir(parents=True, exist_ok=True)
        (STORAGE_ROOT / topic_name / "relevant_papers").mkdir(parents=True, exist_ok=True)

    # ── 创建快捷方式（如果存储路径是外部路径） ──
    if _storage_path_raw and STORAGE_ROOT != BASE_DIR:
        shortcut_file = BASE_DIR / "文献库.lnk"
        if not shortcut_file.exists():
            _create_shortcut(STORAGE_ROOT, shortcut_file)

    # ── 构建主题摘要文本 ──
    topics_summary = " | ".join(t["name"] for t in TOPICS)

    # ── 启动横幅 ──
    console.print(
        Panel(
            f"[bold white]研究方向:[/] [yellow]{topics_summary}[/]\n"
            f"[bold white]目标下载:[/] [green]{TARGET_DOWNLOAD_COUNT} 篇[/]  |  [bold white]种子来源:[/] [cyan]{TODO_DIR}[/]\n"
            f"[bold white]存储根目录:[/] [cyan]{STORAGE_ROOT}[/]",
            title="[bold blue]PaperHarvester — 多主题自动化文献滚雪球检索系统[/]",
            expand=False,
        )
    )

    # ── 加载 API Key ──
    load_dotenv(ENV_PATH, override=True)
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        try:
            raw = ENV_PATH.read_text(encoding="utf-8").strip()
            if raw.startswith("sk-"):
                api_key = raw
            else:
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
    db = PaperDatabase(db_path=DB_PATH)
    fetcher = MetadataFetcher()
    evaluator = RelevanceEvaluator(api_key_str)
    downloader = PaperDownloader(output_dir=STORAGE_ROOT)

    # ── 步骤 1：处理 todo 文件夹中的输入文献（评估 + 分类 + 提取种子DOI） ──
    seed_dois = process_seed_papers(db, evaluator, TOPICS)
    seed_added_count: int = 0
    for doi in seed_dois:
        if db.add_doi(doi):
            seed_added_count = int(seed_added_count) + 1
    if seed_added_count > 0:
        logger.info(f"[green]✓ 新增 {seed_added_count} 个种子 DOI 到数据库[/]")
    elif seed_dois:
        logger.info(f"[dim]种子 DOI 已在数据库中（断点续传模式）[/]")
    else:
        logger.warning(f"[yellow]⚠ todo 文件夹中未提取到种子 DOI，请在 {TODO_DIR} 中放入 PDF 文献[/]")

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
            if MAX_RUNTIME_SEC > 0 and elapsed > MAX_RUNTIME_SEC:
                logger.warning(f"[yellow]⏰ 已达到全局最大运行时间 ({MAX_RUNTIME_SEC}s)，自动退出。[/]")
                break

            # 2c. 检查非活动超时
            if INACTIVITY_TIMEOUT_SEC > 0:
                inactive_duration = time.time() - last_active_time
                if inactive_duration > INACTIVITY_TIMEOUT_SEC:
                    logger.warning(f"[yellow]⏳ 已超过 {INACTIVITY_TIMEOUT_SEC}s 未成功下载，继续下一个...[/]")
                    last_active_time = time.time()

            # ── 周期性热扫描 todo/ 文件夹（每 50 轮检查一次新种子） ──
            if iteration > 0 and iteration % 50 == 0:
                logger.info(f"[cyan]🔄 第 {iteration} 轮：周期性扫描 todo/ 文件夹是否有新种子...[/]")
                hot_seeds = process_seed_papers(db, evaluator, TOPICS)
                if hot_seeds:
                    hot_added = 0
                    for sdoi in hot_seeds:
                        if db.add_doi(sdoi):
                            hot_added += 1
                    if hot_added > 0:
                        logger.info(f"[green]  ✓ 热加载新增 {hot_added} 个种子 DOI 到队列[/]")

            iteration += 1

            # 2d. 获取下一个 pending DOI
            doi = db.get_next_pending()
            if doi is None:
                logger.info("[yellow]📭 队列为空，所有候选文献已处理完毕[/]")
                break

            console.rule(f"[bold]第 {iteration} 轮  |  已下载: {downloaded_count}/{TARGET_DOWNLOAD_COUNT}  |  运行: {int(elapsed)}s")
            logger.info(f"[bold white]📄 处理 DOI: {doi}[/]")

            # 2e. 获取元数据
            title, abstract, ref_dois, is_not_found = fetcher.fetch(doi)

            if is_not_found:
                db.update_paper(doi, status="failed")
                continue

            if title:
                db.update_paper(doi, title=title, abstract=abstract)

            if not title:
                retries = db.increment_retry(doi)
                if retries >= MAX_METADATA_RETRIES:
                    logger.warning(f"[yellow]  ⚠ 元数据获取失败已达 {retries} 次上限，标记为 failed[/]")
                    db.update_paper(doi, status="failed")
                else:
                    logger.warning(f"[yellow]  ⚠ 无法获取元数据，第 {retries}/{MAX_METADATA_RETRIES} 次重试，回退到队尾[/]")
                continue

            # 2f. 多主题评估
            score, best_topic, reason = evaluator.evaluate_multi(title, abstract, TOPICS)

            # 更新评分和归属主题到数据库
            db.update_paper(doi, relevance_score=score, best_topic=best_topic)

            # 2g. 根据评分决定是否下载
            if score >= 5:
                score_dir_name: str = "core_papers" if score >= 7 else "relevant_papers"
                score_output_dir: Path = STORAGE_ROOT / best_topic / score_dir_name
                score_output_dir.mkdir(parents=True, exist_ok=True)
                exit_code = downloader.download(doi, output_dir=score_output_dir)

                success = False
                if exit_code == 0:
                    db.update_paper(doi, status="downloaded")
                    success = True
                    last_active_time = time.time()
                elif exit_code == 2:
                    db.update_paper(doi, status="failed")
                else:
                    logger.info(f"[yellow]  ⚠ 下载失败，标记为 failed，继续下一个[/]")
                    db.update_paper(doi, status="failed")

                # ── 滚雪球逻辑 ──
                current_depth = db.get_depth(doi)
                next_depth = current_depth + 1

                if SNOWBALL_ENABLED and next_depth <= SNOWBALL_MAX_DEPTH:
                    if score >= 7:
                        if ref_dois:
                            newly_added = db.add_dois_batch(ref_dois, depth=next_depth, parent_score=score)
                            logger.info(
                                f"[cyan]  📚 [滚雪球 L{next_depth}] API引用: 共 {len(ref_dois)} 篇, 新增 {newly_added} 篇[/]"
                            )
                        if SNOWBALL_FROM_PDF and success:
                            safe_name = doi.replace("/", "_")
                            safe_name = re.sub(r'[<>:"|?*\\]', "_", safe_name)
                            pdf_file = score_output_dir / f"{safe_name}.pdf"
                            if pdf_file.exists():
                                pdf_dois = extract_dois_from_pdf(pdf_file)
                                pdf_dois = [d for d in pdf_dois if d != doi]
                                if pdf_dois:
                                    pdf_added = db.add_dois_batch(pdf_dois, depth=next_depth, parent_score=score)
                                    logger.info(
                                        f"[cyan]  📚 [滚雪球 L{next_depth}] PDF引用: 共 {len(pdf_dois)} 篇, 新增 {pdf_added} 篇[/]"
                                    )
                    elif score >= 5:
                        if ref_dois:
                            newly_added = db.add_dois_batch(ref_dois, depth=next_depth, parent_score=score)
                            logger.info(
                                f"[cyan]  📚 [滚雪球 L{next_depth}] API引用(精简): 共 {len(ref_dois)} 篇, 新增 {newly_added} 篇[/]"
                            )
                elif not SNOWBALL_ENABLED:
                    logger.info(f"[dim]  ℹ️ 滚雪球已关闭[/]")
                else:
                    logger.info(f"[dim]  ℹ️ 已达最大深度 {SNOWBALL_MAX_DEPTH}[/]")
            else:
                db.update_paper(doi, status="evaluated")
                logger.info(f"[dim]  ⏭ score={score} ≤ 4，跳过下载[/]")

            if iteration % 10 == 0:
                print_status_table(db)

    except KeyboardInterrupt:
        console.print("\n")
        logger.info("[yellow]⚡ 用户中断 (Ctrl+C)，正在优雅退出...[/]")

    # ── 结束统计 ──
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
            f"{'队列为空。' if db.get_next_pending() is None else '可重新运行继续。'}[/]",
            border_style="yellow",
        ))

    db.close()
    logger.info("[dim]数据库连接已关闭，程序退出。[/]")



if __name__ == "__main__":
    main()
