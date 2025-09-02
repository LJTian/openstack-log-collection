from __future__ import annotations

import argparse
import asyncio
import glob
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
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


# 兼容两类 heat-api 记录格式
# A) heat.common.wsgi Processing request: POST /v1/...
API_PROC_LINE = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d{3,6})?).*?"  # 时间
    r"\[\s*(?P<req_id>req-[^\s\]]+)\s+(?P<user_id>[^ \]]+).*?\]\s+"  # req 与宽松的 user_id
    r"Processing\s+request:\s+(?P<method>GET|POST|PUT|PATCH|DELETE)\s+(?P<path>/v1[^\"\s]*)",
    re.IGNORECASE,
)
# B) 通用 eventlet.wsgi.server 访问日志（备用）
WSGI_LINE = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d{3,6})?).*?"  # 时间
    r"\[\s*(?P<req_id>req-[^\s\]]+)\s+(?P<user_id>[^ \]]+).*?\]\s+"  # 放宽用户字段
    r"[0-9a-fA-F:.\-]+\s+-\s+-\s+\[[^\]]+\]\s+\"(?P<method>GET|POST|PUT|PATCH|DELETE)\s+(?P<path>/v1[^\"\s]*)\s+HTTP/1\.1\"",
    re.IGNORECASE,
)


# /v1/<project>/stacks/<stack_name>/<stack_id>/...
STACK_IN_PATH = re.compile(r"/v1/[^/]+/stacks(?:/([^/]+)/(?:([^/\?]+)))?", re.I)


@dataclass
class Row:
    ts: str
    stack_name: str
    stack_id: str
    user_id: str
    action: str


def find_files(patterns: Iterable[str]) -> List[str]:
    files: List[str] = []
    for p in patterns:
        files.extend(glob.glob(p))
    return sorted(set(files))


def classify_action(method: str, path: str, stack_name: Optional[str], stack_id: Optional[str]) -> Optional[str]:
    m = (method or "").upper()
    p = (path or "").lower()
    # 创建：POST /v1/<proj>/stacks （请求行无ID）
    if m == "POST" and "/stacks" in p and stack_id is None:
        return "create"
    # 更新：PUT/PATCH /v1/<proj>/stacks/<name>/<id>
    if m in {"PUT", "PATCH"} and stack_id:
        return "update"
    # 删除：DELETE /v1/<proj>/stacks/<name>/<id>
    if m == "DELETE" and stack_id:
        return "delete"
    return None


ENGINE_REQ = re.compile(r"\[(?P<req_id>req-[^\s\]]+)\]|\[(?P<req_id2>req-[^\s\]]+)\s", re.IGNORECASE)
UUID_RE = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
# heat-engine 名称/ID 提取的多样格式
# 1) Creating stack <name>
ENGINE_CREATING = re.compile(r"Creating\s+stack\s+(?P<name>[A-Za-z0-9_.\-]+)", re.IGNORECASE)
# 2) creating Server "server" Stack "<name>" [<uuid>]
ENGINE_NAME_UUID_SQ = re.compile(
    r"Stack\s+\"(?P<name>[^\"]+)\"\s*\[(?P<uuid>[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\]",
    re.IGNORECASE,
)
# 3) [<name>(<uuid>)] 例如: [ren-test1(926c-...)]
ENGINE_BRACKET_NAME_UUID = re.compile(
    r"\[(?P<name>[^()\[\]]+)\((?P<uuid>[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\)\]",
    re.IGNORECASE,
)
# 4) Stack CREATE/UPDATE/DELETE ... (name)
ENGINE_STATUS_WITH_NAME = re.compile(
    r"Stack\s+(?:CREATE|UPDATE|DELETE|ROLLBACK)\s+(?:IN_PROGRESS|COMPLETE|FAILED)\s+\((?P<name>[^)]+)\)",
    re.IGNORECASE,
)
ENGINE_TS = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d{3,6})?)")


def _parse_dt(ts: str) -> datetime:
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S.%f")
    except Exception:
        return datetime.strptime(normalize_ts(ts), "%Y-%m-%d %H:%M:%S.%f")


def build_engine_correlator(engine_files: List[str]) -> tuple[dict[str, tuple[str, str]], List[dict], List[dict]]:
    """
    扫描 heat-engine.log：
    - 生成 req_id -> (stack_name, stack_id) 的映射（跨行归并：先见 name 后见 uuid 也可）
    - 采集 creating 事件列表：{ts, req_id, name}
    - 采集 name->uuid 事件列表：{ts, name, uuid}
    """
    partial: dict[str, dict] = {}
    id_events: List[dict] = []
    creating_events: List[dict] = []

    for path in engine_files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    # 时间戳
                    mts = ENGINE_TS.search(line)
                    ts = normalize_ts(mts.group("ts")) if mts else ""

                    # req-id（如有）
                    mreq = ENGINE_REQ.search(line)
                    req_id = ""
                    if mreq:
                        req_id = mreq.group("req_id") or mreq.group("req_id2") or ""

                    # 匹配不同格式
                    m_creating = ENGINE_CREATING.search(line)
                    m_sq = ENGINE_NAME_UUID_SQ.search(line)
                    m_br = ENGINE_BRACKET_NAME_UUID.search(line)
                    m_status = ENGINE_STATUS_WITH_NAME.search(line)

                    if req_id:
                        ref = partial.setdefault(req_id, {"name": "", "uuid": "", "ts": ts})
                        if m_creating:
                            ref["name"] = ref["name"] or m_creating.group("name")
                            if ts:
                                creating_events.append({"ts": ts, "req_id": req_id, "name": ref["name"]})
                        if m_sq:
                            ref["name"] = ref["name"] or m_sq.group("name")
                            ref["uuid"] = ref["uuid"] or m_sq.group("uuid")
                        if m_br:
                            ref["name"] = ref["name"] or m_br.group("name")
                            ref["uuid"] = ref["uuid"] or m_br.group("uuid")
                        # 状态行只有名称，可用于补全 name
                        if m_status:
                            ref["name"] = ref["name"] or m_status.group("name")

                    # 采集全局的 name-uuid 事件（不强制 req-id）
                    if m_sq:
                        id_events.append({"ts": ts, "name": m_sq.group("name"), "uuid": m_sq.group("uuid")})
                    if m_br:
                        id_events.append({"ts": ts, "name": m_br.group("name"), "uuid": m_br.group("uuid")})
        except FileNotFoundError:
            continue

    # 构造最终 req 映射（仅含已拿到 uuid 的条目）
    mapping: dict[str, tuple[str, str]] = {}
    for rid, info in partial.items():
        if info.get("uuid"):
            mapping[rid] = (info.get("name", ""), info.get("uuid", ""))

    LOG.info("engine 相关性构建：req映射=%s，creating=%s，id_events=%s", len(mapping), len(creating_events), len(id_events))
    return mapping, creating_events, id_events


def parse_heat_actions(files: List[str], derive_window_sec: int = 600) -> List[Row]:
    rows: List[Row] = []
    api_files: List[str] = []
    engine_files: List[str] = []
    for p in files:
        lp = p.lower()
        if "heat-engine" in lp:
            engine_files.append(p)
        else:
            api_files.append(p)

    engine_corr, creating_events, id_events = build_engine_correlator(engine_files)

    for path in api_files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = API_PROC_LINE.search(line) or WSGI_LINE.search(line)
                    if not m:
                        continue
                    ts = normalize_ts(m.group("ts"))
                    user_id = (m.group("user_id") or "admin").strip() or "admin"
                    req_id = m.group("req_id")
                    method = m.group("method")
                    req_path = m.group("path")

                    name = ""
                    sid = ""
                    sm = STACK_IN_PATH.search(req_path)
                    if sm:
                        name = sm.group(1) or ""
                        sid = sm.group(2) or ""

                    action = classify_action(method, req_path, name or None, sid or None)
                    if action is None:
                        continue
                    # create 无 ID：优先用 engine 的 req 相关性补全
                    if action == "create" and not sid and req_id:
                        hint = engine_corr.get(req_id)
                        if hint:
                            name2, sid2 = hint
                            if name2:
                                name = name or name2
                            sid = sid2 or sid
                    # 兜底：时间窗口弱关联
                    if action == "create" and not sid:
                        try:
                            dt_api = _parse_dt(ts)
                        except Exception:
                            dt_api = None
                        if dt_api is not None:
                            # 选择时间最近的 creating 事件
                            window = timedelta(seconds=int(max(0, derive_window_sec)))
                            nearest = None
                            min_diff = None
                            for ev in creating_events:
                                try:
                                    dt_ev = _parse_dt(ev["ts"]) if ev.get("ts") else None
                                except Exception:
                                    dt_ev = None
                                if not dt_ev:
                                    continue
                                diff = abs((dt_ev - dt_api).total_seconds())
                                if diff <= window.total_seconds():
                                    if min_diff is None or diff < min_diff:
                                        nearest = ev
                                        min_diff = diff
                            if nearest:
                                # 使用 name，并在后续 id 事件中寻找 uuid
                                cname = nearest.get("name") or ""
                                if cname:
                                    name = name or cname
                                    # 在窗口内寻找该 name 的 uuid
                                    sid_candidate = ""
                                    dt_start = _parse_dt(nearest["ts"]) if nearest.get("ts") else dt_api
                                    for ie in id_events:
                                        if (ie.get("name") or "") != cname:
                                            continue
                                        try:
                                            dt_ie = _parse_dt(ie["ts"]) if ie.get("ts") else None
                                        except Exception:
                                            dt_ie = None
                                        if not dt_ie:
                                            continue
                                        if 0 <= (dt_ie - dt_start).total_seconds() <= window.total_seconds():
                                            sid_candidate = ie.get("uuid") or sid_candidate
                                            # 取最早命中即可
                                            break
                                    if sid_candidate:
                                        sid = sid_candidate
                    # 若仍无 ID，也允许入库（以 name 占位，便于后续补全）
                    if action == "create" and not sid:
                        LOG.info("Heat create 无UUID，按名称占位：req=%s name=%s ts=%s", req_id, name, ts)
                    # 若 name 与 id 同时为空，则跳过，不入库
                    if not (name or sid):
                        continue
                    rows.append(Row(ts=ts, stack_name=name, stack_id=sid, user_id=user_id, action=action))
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
            await conn.execute(text(f"ALTER TABLE {table} ADD UNIQUE KEY {index} (stack_name, stack_id, action, ts)"))
            LOG.info("已创建唯一索引：%s(stack_name,stack_id,action,ts)", index)
        else:
            LOG.info("唯一索引已存在：%s", index)


async def write_rows(engine: AsyncEngine, table: str, rows: List[Row]) -> None:
    if not rows:
        LOG.info("无可写入的记录，跳过")
        return
    cols = ["ts", "stack_name", "stack_id", "user_id", "action"]
    sql = f"INSERT IGNORE INTO {table} (" + ", ".join(cols) + ") VALUES (" + ", ".join(f":{c}" for c in cols) + ")"
    params = [{c: getattr(r, c) for c in cols} for r in rows]
    async with engine.begin() as conn:
        await conn.execute(text(sql), params)
    LOG.info("写入完成：表=%s，行数=%s", table, len(rows))


async def run_once(*, dsn: str, table: str, patterns: Iterable[str]) -> None:
    files = find_files(patterns)
    if not files:
        raise FileNotFoundError("未发现 heat-api/heat-api-cfn 日志")
    LOG.info("发现日志 %s 个", len(files))
    rows = parse_heat_actions(files)
    engine = create_async_engine(dsn, pool_pre_ping=True)
    try:
        await ensure_unique_index(engine, table, index="uq_heat_action_ts")
        await write_rows(engine, table, rows)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract Heat stack actions (create/update/delete)")
    parser.add_argument("--dsn", default="mysql+asyncmy://root:pass@127.0.0.1:3306/ops?charset=utf8mb4")
    parser.add_argument("--table", default="heat_action_log")
    parser.add_argument("--pattern", action="append", default=[], help="heat-api/heat-api-cfn log glob pattern (repeatable)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")

    pats = list(args.pattern or [])
    if not pats:
        pats = [
            "/var/log/heat/*heat-api*.log",
            "/var/log/heat/*heat-api-cfn*.log",
            "/var/log/heat/*heat-engine*.log",
        ]

    if args.dry_run:
        files = find_files(pats)
        LOG.info("发现日志 %s 个", len(files))
        rows = parse_heat_actions(files)
        print(f"总记录: {len(rows)}")
        for i, r in enumerate(rows[:50], 1):
            print(f"{i:02d} | ts={r.ts} | stack={r.stack_name} | id={r.stack_id} | user_id={r.user_id} | action={r.action}")
        return

    asyncio.run(run_once(dsn=args.dsn, table=args.table, patterns=pats))


if __name__ == "__main__":
    main()


