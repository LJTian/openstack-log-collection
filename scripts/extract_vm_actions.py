from __future__ import annotations

import argparse
import asyncio
import glob
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy import text


LOG = logging.getLogger(__name__)


# 简单时间规范化：支持 "YYYY-mm-dd HH:MM:SS" 或 "YYYY-mm-dd HH:MM:SS.mmm[mmm]"
def normalize_ts(ts: str) -> str:
    v = (ts or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(v, fmt)
            return dt.strftime("%Y-%m-%d %H:%M:%S.%f")
        except Exception:
            continue
    # 兜底：尽量补齐为 DATETIME(6)
    if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}$", v):
        return v + "000"
    return v


# 解析 nova-api requestlog 行：提取 ts、req_id、user_id、method、path
# 示例：2025-08-25 11:42:58.777 ... nova.api.openstack.requestlog [req-... user project - default default] 10.10.15.35 "GET /v2.1/servers/detail" status: 200 ...
API_LINE = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d{3,6})?).*?"  # 时间
    r"\[\s*(?P<req_id>req-[^\s\]]+)\s+(?P<user_id>[0-9a-f]{32})\s+(?P<project_id>[0-9a-f]{32}).*?\]\s+"  # req 与用户/项目
    r"[0-9a-fA-F:.]+\s+\"(?P<method>GET|POST|DELETE|PUT|PATCH)\s+(?P<path>/[^\"\s]+)",
    re.IGNORECASE,
)


# 解析 nova-compute 行：建立 req_id -> instance 关联
COMPUTE_CORR = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d{3,6})?).*?\[\s*(?P<req_id>req-[^\s\]]+)\s+[0-9a-f]{32}\s+[0-9a-f]{32}.*?\].*?\[instance:\s*(?P<instance>[0-9a-fA-F\-]{36})\]",
    re.IGNORECASE,
)

# 从 compute 日志提取生命周期事件（暂停/恢复），用于反推 API /action 的具体动作
COMPUTE_EVENT = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d{3,6})?).*?\[\s*(?P<req_id>req-[^\s\]]+)[^\]]*\].*?\[instance:\s*(?P<instance>[0-9a-fA-F\-]{36})\].*?VM\s+(?P<ev>Paused|Resumed)\s+\(Lifecycle Event\)",
    re.IGNORECASE,
)


UUID_IN_PATH = re.compile(r"/servers/(?P<uuid>[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})(?:/|$)")


@dataclass
class ActionRow:
    ts: str
    instance: str
    user_id: str
    action: str


def pick_files(patterns: Iterable[str]) -> Tuple[List[str], List[str]]:
    api_files: List[str] = []
    compute_files: List[str] = []
    for pat in patterns:
        for p in glob.glob(pat):
            name = os.path.basename(p).lower()
            if "nova-api" in name:
                api_files.append(p)
            elif "nova-compute" in name:
                compute_files.append(p)
    return sorted(set(api_files)), sorted(set(compute_files))


def build_req_correlator(compute_files: List[str]) -> Tuple[Dict[str, str], Dict[str, str], Dict[str, List[Tuple[datetime, str]]]]:
    req2inst: Dict[str, str] = {}
    req2action: Dict[str, str] = {}
    inst_events: Dict[str, List[Tuple[datetime, str]]] = {}
    for path in compute_files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    m1 = COMPUTE_CORR.search(line)
                    if m1:
                        rid = m1.group("req_id")
                        inst = m1.group("instance")
                        if rid and inst:
                            req2inst[rid] = inst
                    m2 = COMPUTE_EVENT.search(line)
                    if m2:
                        rid2 = m2.group("req_id")
                        ev = (m2.group("ev") or "").lower()
                        inst2 = m2.group("instance")
                        ts_raw = m2.group("ts")
                        try:
                            ts_dt = datetime.strptime(normalize_ts(ts_raw), "%Y-%m-%d %H:%M:%S.%f")
                        except Exception:
                            ts_dt = datetime.min
                        if rid2:
                            if ev == "paused":
                                req2action[rid2] = "pause"
                            elif ev == "resumed":
                                req2action[rid2] = "unpause"
                        if inst2 and ev in {"paused", "resumed"}:
                            act = "pause" if ev == "paused" else "unpause"
                            inst_events.setdefault(inst2, []).append((ts_dt, act))
        except FileNotFoundError:
            continue
    # 排序每个实例的事件序列
    for evs in inst_events.values():
        evs.sort(key=lambda x: x[0])
    LOG.info("构建 req 关联映射完成：req→inst=%s，req→action=%s，inst→events=%s 实例", len(req2inst), len(req2action), len(inst_events))
    return req2inst, req2action, inst_events


def decide_action(method: str, path: str) -> Optional[str]:
    m = (method or "").upper()
    p = (path or "").lower()

    # 创建：POST /servers (不包含 /servers/<uuid>/...)
    if m == "POST" and "/servers" in p and "/servers/" not in p:
        return "create"

    # 打开控制台：POST /servers/<uuid>/remote-consoles
    if m == "POST" and "/servers/" in p and "/remote-consoles" in p:
        return "open_console"

    # 删除（可选）：DELETE /servers/<uuid>
    if m == "DELETE" and UUID_IN_PATH.search(p):
        return "delete"

    return None


def extract_instance_from_path(path: str) -> Optional[str]:
    m = UUID_IN_PATH.search(path or "")
    return m.group("uuid") if m else None


def parse_api_actions(api_files: List[str], req2inst: Dict[str, str], req2derived: Dict[str, str], inst_events: Dict[str, List[Tuple[datetime, str]]], include_delete: bool, derive_window_sec: int = 60) -> List[ActionRow]:
    rows: List[ActionRow] = []
    for path in api_files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = API_LINE.search(line)
                    if not m:
                        continue
                    ts = normalize_ts(m.group("ts"))
                    try:
                        api_dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S.%f")
                    except Exception:
                        api_dt = datetime.min
                    rid = m.group("req_id")
                    user_id = (m.group("user_id") or "admin").strip() or "admin"
                    method = m.group("method")
                    req_path = m.group("path")
                    action = decide_action(method, req_path)

                    # 若为 /action 端点，根据 compute 派生动作（pause/unpause 等）
                    if action is None and method.upper() == "POST" and "/servers/" in req_path.lower() and req_path.lower().endswith("/action"):
                        derived = req2derived.get(rid)
                        if derived in {"pause", "unpause"}:
                            action = derived
                        if action is None:
                            # 尝试基于实例+时间窗口推断
                            inst_from_path = extract_instance_from_path(req_path)
                            if inst_from_path:
                                candidates = inst_events.get(inst_from_path, [])
                                if candidates and api_dt is not datetime.min:
                                    window = timedelta(seconds=max(1, derive_window_sec))
                                    best: Optional[Tuple[datetime, str]] = None
                                    best_delta = None
                                    for t, a in candidates:
                                        if t is datetime.min:
                                            continue
                                        delta = abs((t - api_dt).total_seconds())
                                        if delta <= window.total_seconds():
                                            if best is None or delta < best_delta:  # type: ignore[operator]
                                                best = (t, a)
                                                best_delta = delta
                                    if best is not None:
                                        action = best[1]

                    if action is None:
                        continue
                    if action == "delete" and not include_delete:
                        continue

                    inst = extract_instance_from_path(req_path) or req2inst.get(rid, "")
                    if not inst:
                        # 无法可靠获取实例 ID 时跳过，避免写入无主数据
                        continue
                    rows.append(ActionRow(ts=ts, instance=inst, user_id=user_id, action=action))
        except FileNotFoundError:
            continue
    LOG.info("API 解析完成，提取到 %s 条候选记录", len(rows))
    return rows


async def write_rows(engine: AsyncEngine, table: str, rows: List[ActionRow]) -> None:
    if not rows:
        LOG.info("无可写入的记录，跳过数据库写入")
        return
    cols = ["ts", "instance", "user_id", "action"]
    # 使用 INSERT IGNORE，配合唯一索引避免重复写入
    sql = (
        f"INSERT IGNORE INTO {table} (" + ", ".join(cols) + ") VALUES (" + ", ".join(f":{c}" for c in cols) + ")"
    )
    params = [ {c: getattr(r, c) for c in cols} for r in rows ]
    async with engine.begin() as conn:
        await conn.execute(text(sql), params)
    LOG.info("写入完成：表=%s，行数=%s", table, len(rows))


async def truncate_table(engine: AsyncEngine, table: str) -> None:
    async with engine.begin() as conn:
        try:
            await conn.execute(text(f"TRUNCATE TABLE {table}"))
            LOG.info("已清空数据表（TRUNCATE）：%s", table)
        except Exception:
            # 兼容权限不足的场景，回退 DELETE
            await conn.execute(text(f"DELETE FROM {table}"))
            LOG.info("已清空数据表（DELETE）：%s", table)


async def ensure_unique_index(engine: AsyncEngine, table: str, index_name: str, columns: List[str]) -> None:
    col_list = ", ".join(f"`{c}`" for c in columns)
    async with engine.begin() as conn:
        # 获取当前数据库名
        dbname = await conn.scalar(text("SELECT DATABASE()"))
        if not dbname:
            dbname = "ops"
        # 检查索引是否已存在
        rs = await conn.execute(
            text(
                "SELECT 1 FROM information_schema.statistics "
                "WHERE table_schema=:schema AND table_name=:table AND index_name=:idx LIMIT 1"
            ),
            {"schema": dbname, "table": table, "idx": index_name},
        )
        exists = rs.first() is not None
        if exists:
            LOG.info("唯一索引已存在：%s", index_name)
            return
        await conn.execute(text(f"ALTER TABLE {table} ADD UNIQUE KEY {index_name} ({col_list})"))
        LOG.info("已创建唯一索引：%s(%s)", index_name, ",".join(columns))


async def run_once(
    *,
    dsn: str,
    src_patterns: Iterable[str],
    table: str,
    include_delete: bool,
    derive_window_sec: int = 60,
) -> None:
    api_files, compute_files = pick_files(src_patterns)
    if not api_files and not compute_files:
        raise FileNotFoundError("未发现匹配的 nova 日志文件，请检查配置/路径")

    LOG.info("发现日志：api=%s, compute=%s", len(api_files), len(compute_files))

    req2inst, req2derived, inst_events = build_req_correlator(compute_files)
    rows = parse_api_actions(api_files, req2inst, req2derived, inst_events, include_delete, derive_window_sec)

    engine = create_async_engine(dsn, pool_pre_ping=True)
    try:
        # 确保唯一索引存在，避免重复写入
        await ensure_unique_index(engine, table, index_name="uq_instance_action_ts", columns=["instance", "action", "ts"])
        await write_rows(engine, table, rows)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract VM actions (create/stop[/delete]) from nova logs and write to DB")
    parser.add_argument("--dsn", required=False, default="mysql+asyncmy://root:pass@127.0.0.1:3306/ops?charset=utf8mb4", help="SQLAlchemy async DSN")
    parser.add_argument("--table", required=False, default="nova_compute_action_log", help="Target table name")
    parser.add_argument("--include-delete", action="store_true", help="Also include delete actions")
    parser.add_argument("--pattern", action="append", default=[], help="Log glob pattern (can be repeated)")
    parser.add_argument("--config", required=False, default="config.yaml", help="Fallback to read patterns/dsn from config.yaml if not provided")
    parser.add_argument("--derive-window-sec", type=int, default=60, help="Time window (seconds) to correlate POST /action with compute pause/unpause events by instance")
    parser.add_argument("--dry-run", action="store_true", help="Parse and print without DB writes")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")

    patterns: List[str] = list(args.pattern or [])
    dsn: str = args.dsn

    # 若未提供 pattern，优先从 config.yaml 读取 source.files
    if not patterns and os.path.exists(args.config):
        try:
            import yaml
            with open(args.config, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            for p in (cfg.get("source", {}) or {}).get("files", []) or []:
                patterns.append(p)
            # 若未显式传入 dsn，且 config.yaml 中存在 sinks.db.dsn，则覆盖
            if args.dsn == parser.get_default("dsn"):
                sinks = (cfg.get("sinks", {}) or {}).get("db", {}) or {}
                if "dsn" in sinks and sinks["dsn"]:
                    dsn = sinks["dsn"]
        except Exception as exc:
            LOG.warning("读取配置文件失败（忽略，使用默认/参数）：%s", exc)

    # 若仍无 pattern，使用常见默认路径兜底
    if not patterns:
        patterns = ["/var/log/nova/*nova-api*.log", "/var/log/nova/*nova-compute*.log"]

    # 干跑模式：仅解析并打印摘要
    if args.dry_run:
        api_files, compute_files = pick_files(patterns)
        LOG.info("发现日志：api=%s, compute=%s", len(api_files), len(compute_files))
        req2inst, req2derived, inst_events = build_req_correlator(compute_files)
        rows = parse_api_actions(api_files, req2inst, req2derived, inst_events, include_delete=bool(args.include_delete), derive_window_sec=int(args.derive_window_sec))
        print(f"总记录: {len(rows)}")
        for i, r in enumerate(rows, 1):
            print(f"{i:02d} | ts={r.ts} | instance={r.instance} | user_id={r.user_id} | action={r.action}")
        return

    asyncio.run(run_once(dsn=dsn, src_patterns=patterns, table=args.table, include_delete=bool(args.include_delete), derive_window_sec=int(args.derive_window_sec)))


if __name__ == "__main__":
    main()


