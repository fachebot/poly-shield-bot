#!/usr/bin/env python3
"""一次性迁移脚本：为 tasks 表中 title 为 NULL 的记录回填市场标题。

用法：
    python scripts/backfill_task_titles.py
    python scripts/backfill_task_titles.py --db-path data/poly-shield.db

依赖：
    POLY_DATA_API_URL（Gamma API 地址，通常默认）
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from poly_shield.polymarket import PolymarketGateway
from poly_shield.config import PolymarketCredentials


def get_null_title_tasks(db_path: Path) -> list[tuple[str, str]]:
    """返回 (task_id, token_id) 列表，title 为 NULL。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT task_id, token_id FROM tasks WHERE title IS NULL"
    ).fetchall()
    conn.close()
    return [(r["task_id"], r["token_id"]) for r in rows]


def update_task_title(db_path: Path, task_id: str, title: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE tasks SET title = ? WHERE task_id = ?",
        (title, task_id),
    )
    conn.commit()
    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="回填 tasks 表的 title 字段")
    parser.add_argument(
        "--db-path",
        type=Path,
        default=Path("data") / "poly-shield.db",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.25,
        help="每次 API 调用间隔（秒），避免限速",
    )
    args = parser.parse_args()

    if not args.db_path.exists():
        print(f"数据库不存在: {args.db_path}")
        sys.exit(1)

    tasks = get_null_title_tasks(args.db_path)
    if not tasks:
        print("没有需要回填的记录。")
        sys.exit(0)

    print(f"找到 {len(tasks)} 条 title 为 NULL 的记录，开始回填...")

    try:
        gateway = PolymarketGateway(PolymarketCredentials.from_env())
    except Exception as exc:
        print(f"初始化 PolymarketGateway 失败: {exc}")
        sys.exit(1)

    success = 0
    failed = 0

    for i, (task_id, token_id) in enumerate(tasks, 1):
        print(f"[{i}/{len(tasks)}] 查询 token_id={token_id} ... ", end="", flush=True)
        title = gateway.get_market_title(token_id)
        if title:
            update_task_title(args.db_path, task_id, title)
            print(f"OK -> {title[:50]}{'...' if len(title) > 50 else ''}")
            success += 1
        else:
            print("未找到标题（API 返回空或失败）")
            failed += 1
        time.sleep(args.delay)

    print(f"\n完成：成功 {success}，失败 {failed}")


if __name__ == "__main__":
    main()
