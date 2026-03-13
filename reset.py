#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
reset.py — 一键删除文献下载记忆与缓存（多主题版）
===========================================
运行此脚本将：
1. 删除 SQLite 数据库文件 (papers.db, papers.db-wal, papers.db-shm)
2. 清空所有主题文件夹内已下载的全部 PDF
方便您在修改配置或策略后"从头再来"。
"""

import json
import shutil
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"


def _get_storage_root() -> Path:
    """从 config.json 读取 storage_path，若为空则使用项目目录。"""
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            sp = cfg.get("storage_path", "")
            if sp:
                return Path(sp)
        except Exception:
            pass
    return BASE_DIR


def _get_topics() -> list:
    """从 config.json 读取主题列表。"""
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            return cfg.get("topics", [])
        except Exception:
            pass
    return []


def main():
    storage_root = _get_storage_root()
    topics = _get_topics()
    topic_names = [t.get("name", "") for t in topics if t.get("name")]

    print("==================================================")
    print("⚠️  警告：您正在重置 PaperHarvester 的全部下载记录！")
    print("==================================================")
    print("这将永久删除：")
    print(f"  - 数据库记录: {storage_root / 'papers.db'}")
    if topic_names:
        print(f"  - 以下主题文件夹中的所有 PDF:")
        for name in topic_names:
            print(f"    • {storage_root / name}/")
    else:
        print(f"  - output 目录下的所有 PDF 文件")
    print()

    confirm = input("输入 'y' 确认删除并从头开始，其他任意键取消: ").strip().lower()
    if confirm != 'y':
        print("操作已取消。")
        return

    # 1. 删除数据库文件
    print("\n[1/2] 正在清理数据库缓存...")
    for suffix in ["", "-wal", "-shm"]:
        db_file = storage_root / f"papers.db{suffix}"
        if db_file.exists():
            try:
                db_file.unlink()
                print(f"  ✓ 删除 {db_file.name}")
            except Exception as e:
                print(f"  ✗ 删除 {db_file.name} 失败: {e}")

    # 2. 清理主题目录
    print("\n[2/2] 正在清理下载目录...")
    if topic_names:
        for name in topic_names:
            topic_dir = storage_root / name
            if topic_dir.exists():
                try:
                    shutil.rmtree(topic_dir)
                    topic_dir.mkdir(parents=True, exist_ok=True)
                    (topic_dir / "core_papers").mkdir(exist_ok=True)
                    (topic_dir / "relevant_papers").mkdir(exist_ok=True)
                    print(f"  ✓ 清空并重建 {name}/")
                except Exception as e:
                    print(f"  ✗ 清理 {name}/ 失败: {e}")
    else:
        # 兼容旧版：清理 output 目录
        output_dir = storage_root / "output"
        if output_dir.exists():
            try:
                shutil.rmtree(output_dir)
                output_dir.mkdir(parents=True, exist_ok=True)
                print(f"  ✓ 清空目录 output/")
            except Exception as e:
                print(f"  ✗ 清理 output/ 失败: {e}")

    print("\n==================================================")
    print("✅ 重置完成！您可以重新运行 main.py 开始新的滚雪球下载了。")
    time.sleep(1)

if __name__ == "__main__":
    main()
