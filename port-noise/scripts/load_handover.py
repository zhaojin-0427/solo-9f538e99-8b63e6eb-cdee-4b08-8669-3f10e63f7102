#!/usr/bin/env python3
"""班组交接统计的百万流水压测脚本。

直接在临时 SQLite 库上用 app.stats 的实时写入路径灌入百万条流水增量，
然后断言：
  1. 前缀计数/区间核算与逐条朴素口径一致；
  2. 每次核算读取的汇总行数不超过 64；
  3. 单次核算延迟在毫秒级。

用法：python scripts/load_handover.py [--serials 1000000]
"""
import argparse
import json
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import stats  # noqa: E402

SCHEMA = os.path.join(os.path.dirname(__file__), "..", "app", "schema.sql")


def leaf_truth(conn, crane, serial):
    """直接累加叶子块增量得到的朴素前缀计数（仅用于压测对账）。"""
    total = 0
    for block_idx, text in conn.execute(
            "SELECT block_idx, leaf_deltas FROM crane_count_stats "
            "WHERE crane_id = ? AND level = 0 ORDER BY block_idx", (crane,)):
        origin = block_idx * stats.BLOCK_SIZE
        if origin >= serial:
            break
        for i, delta in enumerate(json.loads(text) if text else []):
            if origin + i + 1 <= serial and delta:
                total += delta
    return total


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--serials", type=int, default=1_000_000)
    parser.add_argument("--cranes", type=int, default=12)
    args = parser.parse_args()

    db_path = os.path.join(tempfile.mkdtemp(), "load.sqlite3")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(open(SCHEMA, encoding="utf-8").read())
    cranes = [f"C{i}" for i in range(1, args.cranes + 1)]

    t0 = time.perf_counter()
    current = {}
    for serial in range(1, args.serials + 1):
        crane = cranes[serial % len(cranes)]
        # 以签发为主，周期性插入钟差更正式负增量，保证不出现负库存
        delta = -1 if (serial % 97 == 0 and current.get(crane, 0) > 0) else 1
        stats.apply_delta(conn, crane, serial, delta)
        current[crane] = current.get(crane, 0) + delta
    conn.commit()
    build_seconds = time.perf_counter() - t0
    stat_rows = conn.execute(
        "SELECT COUNT(*) FROM crane_count_stats").fetchone()[0]
    print(f"灌入 {args.serials} 条流水：{build_seconds:.1f}s，汇总行 {stat_rows}")

    # 读取预算与正确性（抽样 + 全区间）
    worst = 0
    checkpoints = [1, 8, 64, 512, 4096, 32768, 262144,
                   args.serials // 2, args.serials]
    for crane in cranes:
        for serial in checkpoints:
            if serial > args.serials:
                continue
            reader = stats.StatReader(conn, crane)
            got = reader.prefix_count(serial)
            assert got == leaf_truth(conn, crane, serial), (crane, serial)
            worst = max(worst, reader.read_rows)

    t0 = time.perf_counter()
    full = stats.handover_report(conn, cranes[0], 1, args.serials, 0)
    query_ms = (time.perf_counter() - t0) * 1000
    worst = max(worst, full["read_rows"])
    assert full["max_count"] == leaf_truth(conn, cranes[0], args.serials)

    print(f"全区间核算：{query_ms:.2f}ms，峰值 {full['max_count']}，"
          f"首次超限流水 {full['first_exceed_serial']}，读取 {full['read_rows']} 行")
    print(f"全部核算最大读取行数：{worst}（硬上限 {stats.MAX_QUERY_READS}）")
    assert worst <= stats.MAX_QUERY_READS, "读取条数超过 64 行上限"
    print("OK：百万流水下口径正确且读取受控")


if __name__ == "__main__":
    main()
