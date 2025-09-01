from __future__ import annotations

import argparse
import asyncio
import glob
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Optional

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy import text


LOG = logging.getLogger(__name__)


def normalize_ts(ts: str) -> str:
    v = (ts or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(v, fmt)
            return dt.strftime("%Y-%m-%d %H:%M:%S.%f")
        except Exception:
            continue
    if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}$", v):
        return v + "000"
    return v


# eventlet.wsgi.server 日志格式解析（glance-api）
WSGI_LINE = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d{3,6})?).*?"  # 时间
    r"\[\s*(?P<req_id>req-[^\s\]]+)\s+(?P<user_id>[0-9a-f]{32})\s+(?P<project_id>[0-9a-f]{32}).*?\]\s+"  # req + user/project
    r"[0-9a-fA-F:.\-]+\s+-\s+-\s+\[[^\]]+\]\s+\"(?P<method>GET|POST|PUT|PATCH|DELETE)\s+(?P<path>/v2/images[^\"\s]*)",
    re.IGNORECASE,
)


UUID_IN_PATH = re.compile(r"/v2/images/(?P<uuid>[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})(?:/|$)")


@dataclass
class Row:
    ts: str
    image_id: str
    user_id: str
    action: str


def find_files(patterns: Iterable[str]) -> List[str]:
    files: List[str] = []
    for p in patterns:
        files.extend(glob.glob(p))
    return sorted(set(files))


def action_of(method: str, path: str) -> Optional[str]:
    m = (method or "").upper()
    p = (path or "").lower()
    if m == "POST" and p == "/v2/images":
        return "create"
    if m == "PUT" and UUID_IN_PATH.search(p) and p.endswith("/file"):
        return "upload"
    if m == "PATCH" and UUID_IN_PATH.search(p):
        return "update"
    if m == "DELETE" and UUID_IN_PATH.search(p):
        return "delete"
    return None


def extract_id_from_path(path: str) -> Optional[str]:
    m = UUID_IN_PATH.search(path or "")
    return m.group("uuid") if m else None


def parse_glance_actions(files: List[str]) -> List[Row]:
    rows: List[Row] = []
    for path in files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = WSGI_LINE.search(line)
                    if not m:
                        continue
                    ts = normalize_ts(m.group("ts"))
                    user_id = (m.group("user_id") or "admin").strip() or "admin"
                    method = m.group("method")
                    req_path = m.group("path")
                    action = action_of(method, req_path)
                    if action is None:
                        continue
                    image_id = extract_id_from_path(req_path) or ""
                    if action == "create" and not image_id:
                        # create 调用无 ID（后续 response 才返回），此处跳过无法确定 ID 的记录
                        continue
                    if not image_id:
                        continue
                    rows.append(Row(ts=ts, image_id=image_id, user_id=user_id, action=action))
        except FileNotFoundError:
            continue
    LOG.info("解析完成，共 %s 条记录", len(rows))
    return rows


async def ensure_unique_index(engine: AsyncEngine, table: str, index: str, cols: List[str]) -> None:
    col_list = ", ".join(f"`{c}`" for c in cols)
    async with engine.begin() as conn:
        dbname = await conn.scalar(text("SELECT DATABASE()"))
        if not dbname:
            dbname = "ops"
        rs = await conn.execute(text(
            "SELECT 1 FROM information_schema.statistics WHERE table_schema=:s AND table_name=:t AND index_name=:i LIMIT 1"
        ), {"s": dbname, "t": table, "i": index})
        if rs.first() is None:
            await conn.execute(text(f"ALTER TABLE {table} ADD UNIQUE KEY {index} ({col_list})"))
            LOG.info("已创建唯一索引：%s(%s)", index, ",".join(cols))
        else:
            LOG.info("唯一索引已存在：%s", index)


async def write_rows(engine: AsyncEngine, table: str, rows: List[Row]) -> None:
    if not rows:
        LOG.info("无可写入的记录，跳过")
        return
    cols = ["ts", "image_id", "user_id", "action"]
    sql = f"INSERT IGNORE INTO {table} (" + ", ".join(cols) + ") VALUES (" + ", ".join(f":{c}" for c in cols) + ")"
    params = [{c: getattr(r, c) for c in cols} for r in rows]
    async with engine.begin() as conn:
        await conn.execute(text(sql), params)
    LOG.info("写入完成：表=%s，行数=%s", table, len(rows))


async def run_once(*, dsn: str, table: str, patterns: Iterable[str]) -> None:
    files = find_files(patterns)
    if not files:
        raise FileNotFoundError("未发现 glance-api 日志，请检查路径")
    LOG.info("发现日志 %s 个", len(files))
    rows = parse_glance_actions(files)
    engine = create_async_engine(dsn, pool_pre_ping=True)
    try:
        await ensure_unique_index(engine, table, index="uq_image_action_ts", cols=["image_id", "action", "ts"])
        await write_rows(engine, table, rows)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract Glance image actions (create/upload/update/delete)")
    parser.add_argument("--dsn", default="mysql+asyncmy://root:pass@127.0.0.1:3306/ops?charset=utf8mb4")
    parser.add_argument("--table", default="glance_image_action_log")
    parser.add_argument("--pattern", action="append", default=[], help="glance-api log glob pattern (repeatable)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")

    pats = list(args.pattern or [])
    if not pats:
        # 默认尝试常见路径
        pats = ["/var/log/glance/*glance-api*.log"]

    if args.dry_run:
        files = find_files(pats)
        LOG.info("发现日志 %s 个", len(files))
        rows = parse_glance_actions(files)
        print(f"总记录: {len(rows)}")
        for i, r in enumerate(rows, 1):
            print(f"{i:02d} | ts={r.ts} | image={r.image_id} | user_id={r.user_id} | action={r.action}")
        return

    asyncio.run(run_once(dsn=args.dsn, table=args.table, patterns=pats))


if __name__ == "__main__":
    main()


