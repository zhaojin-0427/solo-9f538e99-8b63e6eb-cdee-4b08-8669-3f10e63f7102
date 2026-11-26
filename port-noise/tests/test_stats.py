"""班组交接统计的单元测试：层级汇总口径、读取预算、回填升级。

不依赖 Flask，直接对 app.stats + schema.sql 做白盒验证。
"""
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app import stats  # noqa: E402

SCHEMA = os.path.join(os.path.dirname(__file__), "..", "app", "schema.sql")
CRANES = ["C1", "C2", "C3"]


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(open(SCHEMA, encoding="utf-8").read())
    c.execute("PRAGMA foreign_keys=OFF")
    yield c
    c.close()


class BruteForce:
    """逐条流水的朴素参考实现。"""

    def __init__(self):
        self.current = {}
        self.history = {}

    def apply(self, crane, serial, delta):
        self.current[crane] = self.current.get(crane, 0) + delta
        self.history.setdefault(crane, {})[serial] = self.current[crane]

    def count(self, crane, serial):
        if serial <= 0:
            return 0
        best = 0
        for s in sorted(self.history.get(crane, {})):
            if s <= serial:
                best = self.history[crane][s]
            else:
                break
        return best

    def report(self, crane, lo, hi, limit):
        mx = self.count(crane, lo - 1)
        first = lo if mx > limit else None
        for s in range(lo, hi + 1):
            value = self.count(crane, s)
            mx = max(mx, value)
            if first is None and value > limit:
                first = s
        return mx, first


def _feed(conn, plan):
    """plan: [(crane, serial, delta)]，逐条写入层级汇总。"""
    for crane, serial, delta in plan:
        stats.apply_delta(conn, crane, serial, delta)


def _random_plan(seed, n, cranes=CRANES):
    import random
    rng = random.Random(seed)
    plan = []
    current = {}
    for serial in range(1, n + 1):
        crane = rng.choice(cranes)
        delta = rng.choice([1, 1, 1, -1])
        if delta < 0 and current.get(crane, 0) <= 0:
            delta = 1
        plan.append((crane, serial, delta))
        current[crane] = current.get(crane, 0) + delta
    return plan


def test_prefix_counts_match_bruteforce_across_block_boundaries(conn):
    plan = _random_plan(1, 2000)
    brute = BruteForce()
    for crane, serial, delta in plan:
        stats.apply_delta(conn, crane, serial, delta)
        brute.apply(crane, serial, delta)
    boundaries = [1, 7, 8, 9, 15, 16, 63, 64, 65, 511, 512, 513, 1999, 2000]
    for crane in CRANES:
        for serial in boundaries:
            reader = stats.StatReader(conn, crane)
            assert reader.prefix_count(serial) == brute.count(crane, serial)


def test_range_reports_match_bruteforce(conn):
    plan = _random_plan(2, 1500)
    brute = BruteForce()
    for crane, serial, delta in plan:
        stats.apply_delta(conn, crane, serial, delta)
        brute.apply(crane, serial, delta)
    import random
    rng = random.Random(3)
    for _ in range(400):
        crane = rng.choice(CRANES)
        lo = rng.randint(1, 1500)
        hi = rng.randint(lo, 1500)
        limit = rng.randint(0, 5)
        report = stats.handover_report(conn, crane, lo, hi, limit)
        want_max, want_first = brute.report(crane, lo, hi, limit)
        assert report["max_count"] == want_max
        assert report["first_exceed_serial"] == want_first
        assert report["read_rows"] <= stats.MAX_QUERY_READS


def test_zero_delta_writes_nothing(conn):
    stats.apply_delta(conn, "C1", 1, 0)
    assert conn.execute(
        "SELECT COUNT(*) FROM crane_count_stats").fetchone()[0] == 0


def test_missing_blocks_treated_as_zero_deltas(conn):
    # 只在第 100 条写入：之前所有缺行块必须按零增量处理
    stats.apply_delta(conn, "C1", 100, 1)
    reader = stats.StatReader(conn, "C1")
    assert reader.prefix_count(99) == 0
    assert reader.prefix_count(100) == 1
    report = stats.handover_report(conn, "C1", 1, 100, 0)
    assert report["max_count"] == 1
    assert report["first_exceed_serial"] == 100
    # 起号基线为 0、上限 0：起号本身未超，超限点在 100
    report2 = stats.handover_report(conn, "C1", 50, 99, 0)
    assert report2["max_count"] == 0
    assert report2["first_exceed_serial"] is None


def test_start_serial_already_over_limit_reports_start(conn):
    # 起号前已积累 3 张有效单，上限 2 => 直接报起号
    stats.apply_delta(conn, "C1", 10, 1)
    stats.apply_delta(conn, "C1", 20, 1)
    stats.apply_delta(conn, "C1", 30, 1)
    report = stats.handover_report(conn, "C1", 50, 80, 2)
    assert report["first_exceed_serial"] == 50
    assert report["max_count"] == 3


def test_never_exceeds_is_marked(conn):
    for serial in (10, 20, 30):
        stats.apply_delta(conn, "C1", serial, 1)
    stats.apply_delta(conn, "C1", 40, -2)  # 更正撤单从更正号扣 2
    report = stats.handover_report(conn, "C1", 1, 60, 3)
    assert report["max_count"] == 3
    assert report["first_exceed_serial"] is None
    assert report["read_rows"] <= stats.MAX_QUERY_READS


def test_correction_negative_delta_deducted_at_correction_serial(conn):
    # 1、2 号各 +1；5 号更正撤回 2 张（-2）；6 号再 +1
    stats.apply_delta(conn, "C1", 1, 1)
    stats.apply_delta(conn, "C1", 2, 1)
    stats.apply_delta(conn, "C1", 5, -2)
    stats.apply_delta(conn, "C1", 6, 1)
    reader = stats.StatReader(conn, "C1")
    assert reader.prefix_count(4) == 2
    assert reader.prefix_count(5) == 0   # 撤单量从更正号起一并扣除
    assert reader.prefix_count(6) == 1
    # 同批随后的撤单流水（7、8）沿用更正后的单数，不重复扣减
    assert reader.prefix_count(7) == 1
    assert reader.prefix_count(8) == 1


def test_read_budget_never_exceeds_64(conn):
    plan = _random_plan(4, 50_000)
    _feed(conn, plan)
    import random
    rng = random.Random(5)
    worst = 0
    for _ in range(300):
        crane = rng.choice(CRANES)
        lo = rng.randint(1, 50_000)
        hi = rng.randint(lo, 50_000)
        report = stats.handover_report(conn, crane, lo, hi, rng.randint(0, 20))
        worst = max(worst, report["read_rows"])
    # 全区间查询通常分解最多块
    full = stats.handover_report(conn, "C1", 1, 50_000, 0)
    worst = max(worst, full["read_rows"])
    assert worst <= 64


def test_backfill_rebuilds_matching_stats(conn):
    # 直接构造历史 events/forms（模拟旧卷），再触发回填
    events_forms = [
        ("issue", "C1", None),          # 1
        ("issue", "C1", None),          # 2
        ("issue", "C2", None),          # 3
        ("correction", None, None),     # 4
        ("revoke", "C1", 4),            # 5  撤销 C1 一张，并入 4 号
        ("issue", "C1", None),          # 6
    ]
    form_counter = 0
    active = {}
    for idx, (kind, crane, correction_event) in enumerate(events_forms, start=1):
        if kind == "issue":
            form_counter += 1
            form_no = f"RC-{form_counter:06d}"
            conn.execute(
                "INSERT INTO events(id, kind, detail) VALUES (?, 'issue', '{}')", (idx,))
            conn.execute(
                "INSERT INTO forms(form_no, peak_id, crane_id, status, created_event) "
                "VALUES (?, ?, ?, 'active', ?)", (form_no, f"P{form_counter}", crane, idx))
            active[form_no] = crane
        elif kind == "correction":
            conn.execute(
                "INSERT INTO events(id, kind, detail) VALUES (?, 'correction', '{}')", (idx,))
        else:
            victim = next(f for f, c in active.items() if c == crane)
            del active[victim]
            conn.execute(
                "INSERT INTO events(id, kind, detail) VALUES (?, 'revoke', ?)",
                (idx, json.dumps({"form_no": victim,
                                  "correction_event": correction_event})))
            conn.execute(
                "UPDATE forms SET status='revoked', revoked_event=?, "
                "revoke_reason='钟差更正后该峰不再唯一归属原班组' WHERE form_no=?",
                (idx, victim))

    assert stats.backfill_needed(conn)
    stats.ensure_backfilled(conn)
    assert not stats.backfill_needed(conn)

    reader = stats.StatReader(conn, "C1")
    # 1 号后 1，2 号后 2，4 号更正扣除 1 => 1，5 号撤单不重复扣 => 1，6 号 => 2
    assert reader.prefix_count(1) == 1
    assert reader.prefix_count(3) == 2
    assert reader.prefix_count(4) == 1
    assert reader.prefix_count(5) == 1
    assert reader.prefix_count(6) == 2
    assert stats.StatReader(conn, "C2").prefix_count(6) == 1

    # 幂等：再次启动不重建、不报错
    before = conn.execute("SELECT COUNT(*) FROM crane_count_stats").fetchone()[0]
    stats.ensure_backfilled(conn)
    assert conn.execute(
        "SELECT COUNT(*) FROM crane_count_stats").fetchone()[0] == before


def test_backfill_empty_volume_only_sets_flag(conn):
    # 全新空卷首启：有“待回填”状态，但无流水，ensure 只写标记位不建汇总行
    assert stats.backfill_needed(conn)
    stats.ensure_backfilled(conn)
    assert not stats.backfill_needed(conn)
    assert conn.execute(
        "SELECT value FROM app_meta WHERE key='count_stats_backfilled'").fetchone()[0] == "1"
    assert conn.execute("SELECT COUNT(*) FROM crane_count_stats").fetchone()[0] == 0
    # 标记位存在后，实时写入仍正常工作
    stats.apply_delta(conn, "C1", 1, 1)
    assert stats.StatReader(conn, "C1").prefix_count(1) == 1
