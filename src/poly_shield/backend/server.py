from __future__ import annotations

"""后端服务启动入口。"""

import argparse
from pathlib import Path
from typing import Sequence

import uvicorn

from poly_shield.backend.api import create_app
from poly_shield.backend.runtime import build_default_runtime
from poly_shield.backend.service import DEFAULT_DB_PATH, TaskService


def build_parser() -> argparse.ArgumentParser:
    """构建独立后端服务命令行。"""
    parser = argparse.ArgumentParser(
        prog="poly-shield-api", description="Poly Shield 后端服务")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8787, help="监听端口")
    parser.add_argument(
        "--db-path", default=str(DEFAULT_DB_PATH), help="SQLite 数据库路径")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """启动 FastAPI 服务。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    service = TaskService.from_db_path(Path(args.db_path))
    runtime = build_default_runtime(service)
    app = create_app(service, runtime=runtime)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
