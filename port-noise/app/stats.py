"""班组交接统计：按事件流水核定每台吊机在用复核单数的层级化汇总。

口径
====
* 流水号即 ``events.id``（AUTOINCREMENT），入库先后即流水先后。
* 每笔业务入库后计数：
    - 签发（issue）：对应吊机有效单数 +1；
    - 钟差更正（correction）：该次更正撤回的单从更正号起一并扣除，即按
      吊机把被撤回单数作为负增量计入更正号；同批随后的撤销事件不再重复扣减；
    - 上传（upload）、撤销（revoke）事件本身增量为 0（撤单量已并入更正号）。
* 起号之前发出且仍有效的单，体现在前缀计数里，自然计入区间统计。

存储
====
``crane_count_stats`` 是 8 叉块层级（B=8）：
* level 0 叶子块覆盖 8 条流水，level l 覆盖 8**(l+1) 条；
* 叶子块存块内每条流水的增量 ``leaf_deltas``；
* 每个块存 8 个子块的“块内最高绝对单数” ``child_max`` 与
  “块末绝对单数” ``child_end``，以及块起点前单数 ``base_end_count``；
* 稀疏存储：增量全为 0 的块不建行；父块某子槽为 None 当且仅当该子块确无行。

任意核算都只读 O(log_8 N) 行已入库汇总：百万流水下一次核算读行数
= 前缀层数 + 区间分解层数，始终不超过 64（见 ``MAX_QUERY_READS``）。
"""
from __future__ import annotations

import json

BLOCK_SIZE = 8
MAX_QUERY_READS = 64

# 增量类型（与 events.kind 对齐）
UPLOAD = "upload"
CORRECTION = "correction"
ISSUE = "issue"
REVOKE = "revoke"


# --------------------------------------------------------------------------
# 层级/块换算
# --------------------------------------------------------------------------
def level_span(level):
    """level 层一个块覆盖多少条流水。"""
    return BLOCK_SIZE ** (level + 1)


def block_span(level):
    return level_span(level)


def max_level_for_serial(serial):
    """能容纳 serial 号流水的最高层编号。"""
    n = max(int(serial), 1)
    level = 0
    while level_span(level) < n:
        level += 1
    return level


def _block_origin(level, block_idx):
    return block_idx * block_span(level)


# --------------------------------------------------------------------------
# JSON 稀疏数组工具
# --------------------------------------------------------------------------
def _load_array(text, size):
    if not text:
        return [None] * size
    data = json.loads(text)
    out = [None] * size
    for i, value in enumerate(data[:size]):
        out[i] = value
    return out


def _dump_array(values):
    """去掉尾部 None 后序列化，减小行体积。"""
    trimmed = list(values)
    while trimmed and trimmed[-1] is None:
        trimmed.pop()
    return json.dumps(trimmed, separators=(",", ":"))


# --------------------------------------------------------------------------
# 只读核算器：统计每次核算读取了多少行汇总
# --------------------------------------------------------------------------
class StatReader:
    """针对一次核算请求的只读视图，``read_rows`` 累计实际读取的汇总行数。

    同一 (level, block_idx) 在一次核算内只计一次（块行缓存）。
    """

    def __init__(self, conn, crane_id):
        self.conn = conn
        self.crane_id = crane_id
        self.read_rows = 0
        self._cache = {}

    def _row(self, level, block_idx):
        key = (level, block_idx)
        if key in self._cache:
            return self._cache[key]
        row = self.conn.execute(
            "SELECT base_end_count, child_max, child_end, leaf_deltas "
            "FROM crane_count_stats WHERE crane_id = ? AND level = ? AND block_idx = ?",
            (self.crane_id, level, block_idx),
        ).fetchone()
        self.read_rows += 1
        if row is None:
            self._cache[key] = None
            return None
        parsed = {
            "base_end_count": row[0],
            "child_max": _load_array(row[1], BLOCK_SIZE),
            "child_end": _load_array(row[2], BLOCK_SIZE),
            "leaf_deltas": _load_array(row[3], BLOCK_SIZE),
        }
        self._cache[key] = parsed
        return parsed

    # ----- 前缀计数：count(serial) = serial 号业务入库后的有效单数 ----------
    def prefix_count(self, serial):
        """serial 号业务入库后的有效单数；serial<=0 返回 0。"""
        serial = int(serial)
        if serial <= 0:
            return 0
        level = max_level_for_serial(serial)
        block_idx = (serial - 1) // block_span(level)
        row = self._row(level, block_idx)
        base = row["base_end_count"] if row else 0
        offset = serial - _block_origin(level, block_idx)  # 块内第几条（1 起）
        if level == 0:
            deltas = row["leaf_deltas"] if row else [None] * BLOCK_SIZE
            return base + sum(d for d in deltas[:offset] if d)
        # 高层：先整块累加，再下降到下一层处理剩余部分
        full_slots = (offset - 1) // block_span(level - 1)
        total = base
        if row is not None:
            for slot in range(full_slots):
                end = row["child_end"][slot]
                if end is not None:
                    total = end  # child_end 存绝对单数
        remain = offset - full_slots * block_span(level - 1)
        if remain == 0:
            return total
        child_origin = _block_origin(level, block_idx) + full_slots * block_span(level - 1)
        return self._descend_count(
            level - 1, child_origin // block_span(level - 1), remain, total)

    def _descend_count(self, level, block_idx, offset, base):
        """在已知块起点前单数为 base 的前提下，求块内第 offset 条后的单数。"""
        row = self._row(level, block_idx)
        if row is not None:
            base = row["base_end_count"]
        if level == 0:
            deltas = row["leaf_deltas"] if row else [None] * BLOCK_SIZE
            return base + sum(d for d in deltas[:offset] if d)
        full_slots = (offset - 1) // block_span(level - 1)
        total = base
        if row is not None:
            for slot in range(full_slots):
                end = row["child_end"][slot]
                if end is not None:
                    total = end
        remain = offset - full_slots * block_span(level - 1)
        if remain == 0:
            return total
        child_origin = _block_origin(level, block_idx) + full_slots * block_span(level - 1)
        return self._descend_count(
            level - 1, child_origin // block_span(level - 1), remain, total)

    # ----- 区间核算：最高单数 + 首次超限流水号 --------------------------------
    def range_report(self, start_serial, end_serial, limit):
        """返回 dict：max_count、first_exceed_serial（无则 None）、read_rows。

        先核起号本身（起号已超限即报起号）；再沿 8 叉块分解区间，
        整块用 child_max 判定，部分块下降处理。每读一行汇总计 1 次读取。
        """
        start_serial = int(start_serial)
        end_serial = int(end_serial)
        before_start = self.prefix_count(start_serial - 1)
        running = before_start
        max_count = before_start  # 起号前的基线也要参与“同时有效最多”比较
        first_exceed = start_serial if running > limit else None
        state = _ScanState(running, max_count, first_exceed)
        self._scan_state = state
        top_level = max_level_for_serial(end_serial)
        top_span = block_span(top_level)
        first_block = (start_serial - 1) // top_span
        last_block = (end_serial - 1) // top_span
        for block_idx in range(first_block, last_block + 1):
            origin = block_idx * top_span
            block_lo = max(start_serial, origin + 1)
            block_hi = min(end_serial, origin + top_span)
            self._scan_block(top_level, block_idx, block_lo, block_hi, limit)
        return {
            "max_count": state.max_count,
            "first_exceed_serial": state.first_exceed,
            "read_rows": self.read_rows,
        }

    def _scan_block(self, level, block_idx, lo, hi, limit):
        """扫描 [lo, hi]（均为全局流水号，且落在本块内），更新状态。"""
        state = self._scan_state
        row = self._row(level, block_idx)
        loff = lo - _block_origin(level, block_idx)
        hoff = hi - _block_origin(level, block_idx)
        if level == 0:
            if row is None:
                # 缺行：块内全是零增量，单数恒为块起点前的前缀值，
                # 即上层传入的 state.running；不得把它再当 base 叠加
                running = state.running
                deltas = [None] * BLOCK_SIZE
            else:
                deltas = row["leaf_deltas"]
                if loff == 1:
                    # 从块首扫描：直接用块行记录的块起点前单数
                    running = row["base_end_count"]
                else:
                    # 块中扫描：以上层传入的当前单数为准，只叠加本区段的增量
                    running = state.running
            for offset in range(loff, hoff + 1):
                running += deltas[offset - 1] or 0
                state.running = running
                if running > state.max_count:
                    state.max_count = running
                if state.first_exceed is None and running > limit:
                    state.first_exceed = _block_origin(level, block_idx) + offset
            return
        child_span = block_span(level - 1)
        first_slot = (loff - 1) // child_span
        last_slot = (hoff - 1) // child_span
        for slot in range(first_slot, last_slot + 1):
            slot_origin = _block_origin(level, block_idx) + slot * child_span
            child_lo = max(lo, slot_origin + 1)
            child_hi = min(hi, slot_origin + child_span)
            full_cover = (child_lo == slot_origin + 1
                          and child_hi == slot_origin + child_span)
            if full_cover and row is not None and row["child_max"][slot] is not None:
                # 整块覆盖：直接用已入库的绝对单数汇总，无需下降
                slot_max = row["child_max"][slot]
                state.max_count = max(state.max_count, slot_max)
                if state.first_exceed is None and slot_max > limit:
                    # 块内含超限点，下降定位首条（多读若干层，仍受 64 行约束）
                    self._locate_first_exceed(
                        level - 1, slot_origin // child_span, limit)
                state.running = row["child_end"][slot]
            else:
                self._scan_block(
                    level - 1, slot_origin // child_span, child_lo, child_hi, limit)

    def _locate_first_exceed(self, level, block_idx, limit):
        """已知该块内存在超限流水，下降定位最小流水号。

        child_max/child_end 均为绝对单数；调用方已据 child_max 确认块内含超限点。
        """
        state = self._scan_state
        row = self._row(level, block_idx)
        if row is None:
            return  # 缺行则块内单数恒定，不可能含超限点
        if level == 0:
            running = row["base_end_count"]
            for offset, delta in enumerate(row["leaf_deltas"], start=1):
                running += delta or 0
                if running > limit:
                    serial = _block_origin(level, block_idx) + offset
                    if state.first_exceed is None or serial < state.first_exceed:
                        state.first_exceed = serial
                    return
            return
        for slot in range(BLOCK_SIZE):
            slot_max = row["child_max"][slot]
            if slot_max is not None and slot_max > limit:
                child_origin = _block_origin(level, block_idx) + slot * block_span(level - 1)
                self._locate_first_exceed(
                    level - 1, child_origin // block_span(level - 1), limit)
                return
            if row["child_end"][slot] is not None:
                state.running = row["child_end"][slot]


class _ScanState:
    def __init__(self, running, max_count, first_exceed):
        self.running = running
        self.max_count = max_count
        self.first_exceed = first_exceed


# --------------------------------------------------------------------------
# 写入路径：与签发/更正同事务调用
# --------------------------------------------------------------------------
def apply_delta(conn, crane_id, serial, delta):
    """把某吊机在 serial 号流水上的单数增量 delta 写入层级汇总。

    必须与业务写入处于同一事务：保存失败整体回滚，钟差、单据、统计都照旧。
    delta=0 直接跳过（不产生任何汇总行）。
    """
    if not delta:
        return
    serial = int(serial)
    top_level = max_level_for_serial(serial)

    # ---- 叶子块 ----
    leaf_idx = (serial - 1) // BLOCK_SIZE
    leaf_origin = leaf_idx * BLOCK_SIZE
    row = conn.execute(
        "SELECT leaf_deltas, base_end_count FROM crane_count_stats "
        "WHERE crane_id = ? AND level = 0 AND block_idx = ?",
        (crane_id, leaf_idx),
    ).fetchone()
    if row is None:
        base = _base_before_block(conn, crane_id, 0, leaf_idx)
        deltas = [None] * BLOCK_SIZE
    else:
        base = row[1]
        deltas = _load_array(row[0], BLOCK_SIZE)
    slot = serial - leaf_origin - 1
    deltas[slot] = (deltas[slot] or 0) + delta
    _upsert_leaf(conn, crane_id, leaf_idx, base, deltas)

    # ---- 各层祖先块 ----
    for level in range(1, top_level + 1):
        span = block_span(level)
        block_idx = (serial - 1) // span
        origin = block_idx * span
        child_span = block_span(level - 1)
        child_slot = (serial - 1 - origin) // child_span
        prow = conn.execute(
            "SELECT base_end_count, child_max, child_end FROM crane_count_stats "
            "WHERE crane_id = ? AND level = ? AND block_idx = ?",
            (crane_id, level, block_idx),
        ).fetchone()
        if prow is None:
            pbase = _base_before_block(conn, crane_id, level, block_idx)
            child_max = [None] * BLOCK_SIZE
            child_end = [None] * BLOCK_SIZE
            # 惰性建块：此前已入库的兄弟子块一并汇总，
            # 保证 None 只代表“该子槽确无块行”，不会把已有数据误当零增量
            for slot in range(BLOCK_SIZE):
                sibling_idx = origin // child_span + slot
                srow = conn.execute(
                    "SELECT base_end_count, child_max, child_end, leaf_deltas "
                    "FROM crane_count_stats WHERE crane_id = ? AND level = ? AND block_idx = ?",
                    (crane_id, level - 1, sibling_idx),
                ).fetchone()
                if srow is not None:
                    smax, send = _summarize_child(srow, level - 1)
                    child_max[slot] = smax
                    child_end[slot] = send
        else:
            pbase = prow[0]
            child_max = _load_array(prow[1], BLOCK_SIZE)
            child_end = _load_array(prow[2], BLOCK_SIZE)
        # 重算受影响子槽（写入路径不计查询读取预算）
        child_block_idx = origin // child_span + child_slot
        crow = conn.execute(
            "SELECT base_end_count, child_max, child_end, leaf_deltas "
            "FROM crane_count_stats WHERE crane_id = ? AND level = ? AND block_idx = ?",
            (crane_id, level - 1, child_block_idx),
        ).fetchone()
        cmax, cend = _summarize_child(crow, level - 1)
        child_max[child_slot] = cmax
        child_end[child_slot] = cend
        conn.execute(
            "INSERT INTO crane_count_stats(crane_id, level, block_idx, base_end_count, "
            "child_max, child_end, leaf_deltas) VALUES (?, ?, ?, ?, ?, ?, '[]') "
            "ON CONFLICT(crane_id, level, block_idx) DO UPDATE SET "
            "base_end_count = excluded.base_end_count, child_max = excluded.child_max, "
            "child_end = excluded.child_end",
            (crane_id, level, block_idx, pbase,
             _dump_array(child_max), _dump_array(child_end)),
        )


def _upsert_leaf(conn, crane_id, block_idx, base, deltas):
    conn.execute(
        "INSERT INTO crane_count_stats(crane_id, level, block_idx, base_end_count, "
        "child_max, child_end, leaf_deltas) VALUES (?, 0, ?, ?, '[]', '[]', ?) "
        "ON CONFLICT(crane_id, level, block_idx) DO UPDATE SET "
        "base_end_count = excluded.base_end_count, leaf_deltas = excluded.leaf_deltas",
        (crane_id, block_idx, base, _dump_array(deltas)),
    )


def _summarize_child(row, level):
    """由子块行求 (块内最高绝对单数, 块末绝对单数)；缺行返回 (0, 0)。

    child_max/child_end 统一存绝对单数（不是相对子块 base 的增量），
    读取侧可直接与上限/基线比较。
    """
    if row is None:
        return 0, 0
    base = row[0]
    if level == 0:
        deltas = _load_array(row[3], BLOCK_SIZE)
        running = base
        peak = base
        for delta in deltas:
            if delta:
                running += delta
                peak = max(peak, running)
        return peak, running
    cmax = [v for v in _load_array(row[1], BLOCK_SIZE) if v is not None]
    peak = max(cmax) if cmax else base
    ends = [v for v in _load_array(row[2], BLOCK_SIZE) if v is not None]
    end = ends[-1] if ends else base
    return peak, end


def _base_before_block(conn, crane_id, level, block_idx):
    """求 (level, block_idx) 块起点前的有效单数。

    惰性建块时该块自身尚不存在，而更早的同层块可能存在。直接沿同层块号
    向前找到最近的已建块：其块末绝对单数（child_end 末槽，叶子用 base+增量）
    就是本块起点前单数；一路都没有则为 0。这样不依赖尚在构建中的祖先路径，
    避免“祖先缺行导致前缀误算为 0”的问题。
    """
    for prev in range(block_idx - 1, -1, -1):
        row = conn.execute(
            "SELECT base_end_count, child_max, child_end, leaf_deltas "
            "FROM crane_count_stats WHERE crane_id = ? AND level = ? AND block_idx = ?",
            (crane_id, level, prev),
        ).fetchone()
        if row is None:
            continue
        peak, end = _summarize_child(row, level)
        return end
    return 0


# --------------------------------------------------------------------------
# 旧 SQLite 卷升级：补齐历史统计
# --------------------------------------------------------------------------
def backfill_needed(conn):
    """是否还有未完成的历史回填：仅看标记位。

    新卷首启时没有标记（即便没有流水），需要补一个标记位；
    已回填（含空卷标记）后恒为 False。
    """
    flag = conn.execute(
        "SELECT value FROM app_meta WHERE key = 'count_stats_backfilled'").fetchone()
    return flag is None


def ensure_backfilled(conn):
    """应用启动时调用：对旧卷的历史流水一次性补齐层级统计。

    新卷（无流水）只写标记位。回填在单事务内完成，失败回滚不留半成品。
    """
    if not backfill_needed(conn):
        return 0
    return _backfill(conn)


def _backfill(conn):
    max_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
    if max_id == 0:
        # 空的新卷：标记为已回填，后续首笔业务直接走实时写入
        _mark_backfilled(conn)
        conn.commit()
        return 0

    # 1) 从已入库的 events/forms 重建每条流水的增量（按吊机分组）
    deltas = _collect_historical_deltas(conn)

    # 2) 自底向上批量建块。先叶子层，再逐层由子块汇总父块。
    for crane_id in sorted(deltas.keys()):
        _build_leaf_level(conn, crane_id, deltas[crane_id])
        level = 1
        while block_span(level - 1) < max_id:
            _build_level_from_children(conn, crane_id, level, max_id)
            level += 1

    _mark_backfilled(conn)
    conn.commit()
    return sum(len(v) for v in deltas.values())


def _collect_historical_deltas(conn):
    """扫描历史 events/forms，返回 {crane_id: {serial: delta}}。

    口径与实时写入一致：issue +1；correction 按吊机计入被撤回单数（负值）；
    upload/revoke 不产生增量（撤单量已并入更正号）。

    全程集合化聚合（两条 GROUP BY 查询），不做逐事件点查，百万流水也是线性。
    """
    deltas = {}

    def add(crane_id, serial, delta):
        deltas.setdefault(crane_id, {})
        deltas[crane_id][serial] = deltas[crane_id].get(serial, 0) + delta

    # 签发增量：每个 issue 事件对应的一张单，按吊机 +1
    for crane_id, serial in conn.execute(
        "SELECT f.crane_id, f.created_event FROM forms f "
        "JOIN events e ON e.id = f.created_event AND e.kind = 'issue'"
    ):
        add(crane_id, serial, 1)

    # 更正增量：同次更正撤回的单按吊机汇总，负增量计入更正号（不是撤单号）
    for crane_id, correction_event, amount in conn.execute(
        "SELECT f.crane_id, json_extract(e.detail, '$.correction_event'), COUNT(*) "
        "FROM forms f JOIN events e ON e.id = f.revoked_event "
        "WHERE e.kind = 'revoke' "
        "AND json_extract(e.detail, '$.correction_event') IS NOT NULL "
        "GROUP BY f.crane_id, json_extract(e.detail, '$.correction_event')"
    ):
        add(crane_id, int(correction_event), -int(amount))
    return deltas


def _build_leaf_level(conn, crane_id, crane_deltas):
    if not crane_deltas:
        return
    max_serial = max(crane_deltas)
    last_block = (max_serial - 1) // BLOCK_SIZE
    running = 0  # 已处理流水的累计绝对单数；叶子层按块序构建，无需回查
    rows = []
    for block_idx in range(last_block + 1):
        origin = block_idx * BLOCK_SIZE
        base = running
        values = [None] * BLOCK_SIZE
        touched = False
        for serial in range(origin + 1, origin + BLOCK_SIZE + 1):
            delta = crane_deltas.get(serial)
            if delta:
                values[serial - origin - 1] = delta
                running += delta
                touched = True
        if touched:
            rows.append((crane_id, 0, block_idx, base,
                         "[]", "[]", _dump_array(values)))
    conn.executemany(
        "INSERT INTO crane_count_stats(crane_id, level, block_idx, base_end_count, "
        "child_max, child_end, leaf_deltas) VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )


def _build_level_from_children(conn, crane_id, level, max_serial):
    span = block_span(level)
    child_span = block_span(level - 1)
    last_block = (max_serial - 1) // span
    # 一次取出该吊机全部子块行（升级期批量操作，不计查询读取预算）
    children = {
        row[0]: row for row in conn.execute(
            "SELECT block_idx, base_end_count, child_max, child_end, leaf_deltas "
            "FROM crane_count_stats WHERE crane_id = ? AND level = ?",
            (crane_id, level - 1),
        )
    }
    if not children:
        return
    running = 0  # 已处理子块的块末绝对单数；父块同样按块序构建
    rows = []
    for block_idx in range(last_block + 1):
        origin = block_idx * span
        base = running
        child_max = [None] * BLOCK_SIZE
        child_end = [None] * BLOCK_SIZE
        touched = False
        for slot in range(BLOCK_SIZE):
            child_block_idx = origin // child_span + slot
            crow = children.get(child_block_idx)
            if crow is None:
                continue
            # children 查询列序：block_idx(0), base_end_count(1), child_max(2),
            # child_end(3), leaf_deltas(4)
            abs_max, abs_end = _summarize_child(
                (crow[1], crow[2], crow[3], crow[4]), level - 1)
            child_max[slot] = abs_max
            child_end[slot] = abs_end
            running = abs_end
            touched = True
        if touched:
            rows.append((crane_id, level, block_idx, base,
                         _dump_array(child_max), _dump_array(child_end), "[]"))
    conn.executemany(
        "INSERT INTO crane_count_stats(crane_id, level, block_idx, base_end_count, "
        "child_max, child_end, leaf_deltas) VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )


def _mark_backfilled(conn):
    conn.execute(
        "INSERT INTO app_meta(key, value) VALUES ('count_stats_backfilled', '1') "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value")


# --------------------------------------------------------------------------
# 对外核算入口
# --------------------------------------------------------------------------
def handover_report(conn, crane_id, start_serial, end_serial, limit):
    """班组交接核算。返回含 max_count / first_exceed_serial / read_rows 的 dict。"""
    reader = StatReader(conn, crane_id)
    report = reader.range_report(start_serial, end_serial, limit)
    if report["read_rows"] > MAX_QUERY_READS:  # 硬性自检：永不超 64 行
        raise RuntimeError(
            f"核算读取 {report['read_rows']} 行汇总，超过上限 {MAX_QUERY_READS}")
    report["crane_id"] = crane_id
    report["start_serial"] = start_serial
    report["end_serial"] = end_serial
    report["limit"] = limit
    return report
