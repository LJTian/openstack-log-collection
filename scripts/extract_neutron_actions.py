from __future__ import annotations

import argparse
import asyncio
import glob
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, List, Optional

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


# neutron.wsgi 日志行解析
WSGI_LINE = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d{3,6})?).*?"  # 时间
    r"\[\s*(?P<req_id>req-[^\s\]]+)\s+(?P<user_id>[0-9a-f]{32})\s+(?P<project_id>[0-9a-f]{32}).*?\]\s+"  # req 与用户/项目
    r"[0-9a-fA-F:.\-]+\s+\"(?P<method>GET|POST|PUT|PATCH|DELETE)\s+(?P<path>/v2\.0/[^\"\s]+)\s+HTTP/1\.1\"",
    re.IGNORECASE,
)


RES_ID_RE = re.compile(r"^/v2\.0/(?P<resource>[a-z_]+)(?:/(?P<rid>[0-9a-f\-]+))?(?P<rest>/.*)?$", re.I)


@dataclass
class Row:
    ts: str
    resource: str
    resource_id: str
    user_id: str
    action: str


def find_files(patterns: Iterable[str]) -> List[str]:
    files: List[str] = []
    for p in patterns:
        files.extend(glob.glob(p))
    return sorted(set(files))


def classify_action(method: str, resource: str, rid: Optional[str], rest: str) -> Optional[str]:
    m = (method or "").upper()
    res = (resource or "").lower()
    r = rest or ""
    if m == "POST" and rid is None:
        return "create"
    if m in {"PUT", "PATCH"} and rid:
        # 端口绑定相关
        if res == "ports" and "/bindings" in r:
            if m in {"PUT", "PATCH"} and r.endswith("/activate"):
                return "binding_activate"
            return "binding_update"
        return "update"
    if m == "DELETE" and rid:
        if res == "ports" and "/bindings" in r:
            return "binding_delete"
        return "delete"
    return None


def parse_neutron_actions(files: List[str]) -> List[Row]:
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
                    p = m.group("path")

                    m2 = RES_ID_RE.match(p)
                    if not m2:
                        continue
                    resource = (m2.group("resource") or "").lower()
                    rid = m2.group("rid") or ""
                    rest = m2.group("rest") or ""

                    action = classify_action(method, resource, rid or None, rest)
                    if action is None:
                        continue
                    # create 时没有 ID，API 响应体才返回，此处无法稳定获取，跳过无 ID 的记录
                    if not rid:
                        continue
                    rows.append(Row(ts=ts, resource=resource, resource_id=rid, user_id=user_id, action=action))
        except FileNotFoundError:
            continue
    LOG.info("解析完成，共 %s 条记录", len(rows))
    return rows


async def ensure_unique_index(engine: AsyncEngine, table: str, index: str) -> None:
    async with engine.begin() as conn:
        dbname = await conn.scalar(text("SELECT DATABASE()"))
        if not dbname:
            dbname = "ops"
        rs = await conn.execute(text(
            "SELECT 1 FROM information_schema.statistics WHERE table_schema=:s AND table_name=:t AND index_name=:i LIMIT 1"
        ), {"s": dbname, "t": table, "i": index})
        if rs.first() is None:
            await conn.execute(text(f"ALTER TABLE {table} ADD UNIQUE KEY {index} (resource, resource_id, action, ts)"))
            LOG.info("已创建唯一索引：%s(resource,resource_id,action,ts)", index)
        else:
            LOG.info("唯一索引已存在：%s", index)


async def write_rows(engine: AsyncEngine, table: str, rows: List[Row]) -> None:
    if not rows:
        LOG.info("无可写入的记录，跳过")
        return
    cols = ["ts", "resource", "resource_id", "user_id", "action"]
    sql = f"INSERT IGNORE INTO {table} (" + ", ".join(cols) + ") VALUES (" + ", ".join(f":{c}" for c in cols) + ")"
    params = [{c: getattr(r, c) for c in cols} for r in rows]
    async with engine.begin() as conn:
        await conn.execute(text(sql), params)
    LOG.info("写入完成：表=%s，行数=%s", table, len(rows))


async def run_once(*, dsn: str, table: str, patterns: Iterable[str]) -> None:
    files = find_files(patterns)
    if not files:
        raise FileNotFoundError("未发现 neutron-server 日志")
    LOG.info("发现日志 %s 个", len(files))
    rows = parse_neutron_actions(files)
    engine = create_async_engine(dsn, pool_pre_ping=True)
    try:
        await ensure_unique_index(engine, table, index="uq_neutron_action_ts")
        await write_rows(engine, table, rows)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract Neutron network actions (create/update/delete/bindings)")
    parser.add_argument("--dsn", default="mysql+asyncmy://root:pass@127.0.0.1:3306/ops?charset=utf8mb4")
    parser.add_argument("--table", default="neutron_action_log")
    parser.add_argument("--pattern", action="append", default=[], help="neutron-server log glob pattern (repeatable)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")

    pats = list(args.pattern or [])
    if not pats:
        pats = ["/var/log/neutron/*neutron-server*.log"]

    if args.dry_run:
        files = find_files(pats)
        LOG.info("发现日志 %s 个", len(files))
        rows = parse_neutron_actions(files)
        print(f"总记录: {len(rows)}")
        for i, r in enumerate(rows[:50], 1):
            print(f"{i:02d} | ts={r.ts} | resource={r.resource} | id={r.resource_id} | user_id={r.user_id} | action={r.action}")
        return

    asyncio.run(run_once(dsn=args.dsn, table=args.table, patterns=pats))


if __name__ == "__main__":
    main()


