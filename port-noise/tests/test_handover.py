"""班组交接核算的端到端 API 测试：页面/接口口径、读取条数、事务一致性、旧卷升级。"""
import sqlite3

from tests.conftest import standard_clocks, standard_peaks, standard_work, upload


def _issue(client, peak_id):
    return client.post("/api/forms", json={"peak_id": peak_id})


def test_handover_page_requires_upload(client):
    resp = client.get("/handover")
    assert resp.status_code == 302
    assert "/upload" in resp.headers["Location"]


def test_handover_page_renders_form(loaded_client):
    page = loaded_client.get("/handover")
    assert page.status_code == 200
    assert "班组交接" in page.data.decode()
    assert "C1" in page.data.decode()


def test_handover_api_basic_counts(loaded_client):
    client = loaded_client
    # 标准数据：P1、P7 可签 C1
    r1 = _issue(client, "P1").get_json()
    r2 = _issue(client, "P7").get_json()
    e1, e2 = r1["event_id"], r2["event_id"]
    report = client.get(
        f"/api/handover?crane_id=C1&start_serial={e1}&end_serial={e2}&limit=1"
    ).get_json()
    assert report["max_count"] == 2
    assert report["first_exceed_serial"] == e2
    assert report["exceeded"] is True
    assert 1 <= report["read_rows"] <= 64
    assert report["max_query_reads"] == 64


def test_handover_never_exceeds_marks_it(loaded_client):
    client = loaded_client
    r = _issue(client, "P1").get_json()
    report = client.get(
        f"/api/handover?crane_id=C1&start_serial={r['event_id']}"
        f"&end_serial={r['event_id']}&limit=5"
    ).get_json()
    assert report["max_count"] == 1
    assert report["first_exceed_serial"] is None
    assert report["exceeded"] is False


def test_handover_start_already_over_limit_reports_start(loaded_client):
    client = loaded_client
    r1 = _issue(client, "P1").get_json()
    r2 = _issue(client, "P7").get_json()
    # 区间从第二张单所在流水开始：该流水入库后已有 2 张有效单，上限 1 => 报该流水
    start = r2["event_id"]
    report = client.get(
        f"/api/handover?crane_id=C1&start_serial={start}"
        f"&end_serial={start}&limit=1"
    ).get_json()
    assert report["first_exceed_serial"] == start
    assert report["max_count"] == 2


def test_handover_correction_deducts_at_correction_serial(app, loaded_client):
    client = loaded_client
    f1 = _issue(client, "P1").get_json()
    # 更正 r1 使 P1 失去 C1 归属 => 撤单，负增量并入更正号
    corr = client.post("/api/clock-records/1/correct",
                       json={"low_sec": 100, "high_sec": 200}).get_json()
    correction_event = corr["correction_event"]
    # 更正号之前 C1 有 1 张有效单
    before = client.get(
        f"/api/handover?crane_id=C1&start_serial={f1['event_id']}"
        f"&end_serial={correction_event - 1}&limit=0"
    ).get_json()
    assert before["max_count"] == 1
    assert before["first_exceed_serial"] == f1["event_id"]
    # 更正号起单数被扣为 0；随后的撤单流水沿用更正后的单数，不重复扣减
    # 直接核对各流水入库后的有效单数（前缀计数口径）
    import sqlite3
    with sqlite3.connect(app.config["DATABASE"]) as raw:
        raw.row_factory = sqlite3.Row
        from app import stats as stats_mod
        reader = stats_mod.StatReader(raw, "C1")
        assert reader.prefix_count(f1["event_id"] - 1) == 0
        assert reader.prefix_count(f1["event_id"]) == 1      # 签发后 1 张
        assert reader.prefix_count(correction_event - 1) == 1
        assert reader.prefix_count(correction_event) == 0    # 撤单量并入更正号扣除
        revoke_event = correction_event + 1
        assert reader.prefix_count(revoke_event) == 0        # 撤单流水不重复扣

    # 区间严格落在更正之后：起号基线已是 0，区间内始终 0、不超限
    after = client.get(
        f"/api/handover?crane_id=C1&start_serial={correction_event + 1}"
        f"&end_serial={correction_event + 1}&limit=0"
    ).get_json()
    assert after["max_count"] == 0
    assert after["first_exceed_serial"] is None
    # 跨更正号的区间：峰值取更正前的 1，更正号之后回落到 0
    across = client.get(
        f"/api/handover?crane_id=C1&start_serial={f1['event_id']}"
        f"&end_serial={correction_event + 1}&limit=0"
    ).get_json()
    assert across["max_count"] == 1
    assert across["first_exceed_serial"] == f1["event_id"]


def test_handover_stats_roll_back_with_business_write(app, loaded_client):
    """保存失败时钟差、单据和统计都照旧：用触发器让统计写入失败，验证同事务回滚。"""
    app.config["TESTING"] = False  # 让 500 错误处理器产生响应而非抛出
    client = loaded_client
    with sqlite3.connect(app.config["DATABASE"]) as raw:
        raw.execute("PRAGMA foreign_keys=ON")
        # 建一个会触发统计写入约束失败的触发器
        raw.execute(
            "CREATE TRIGGER fail_stats BEFORE INSERT ON crane_count_stats "
            "BEGIN SELECT RAISE(ABORT, 'injected stats failure'); END")
        raw.commit()
    resp = _issue(client, "P1")
    assert resp.status_code == 500
    assert resp.get_json()["error"]
    # 事件、单据、统计全部回滚，库内照旧
    with sqlite3.connect(app.config["DATABASE"]) as raw:
        assert raw.execute("SELECT COUNT(*) FROM forms").fetchone()[0] == 0
        assert raw.execute(
            "SELECT COUNT(*) FROM crane_count_stats").fetchone()[0] == 0
        assert raw.execute(
            "SELECT COUNT(*) FROM events WHERE kind='issue'").fetchone()[0] == 0
        # 业务连接仍健康：移除触发器后可正常签发且统计随之入库
        raw.execute("DROP TRIGGER fail_stats")
        raw.commit()
    ok = _issue(client, "P1")
    assert ok.status_code == 201
    with sqlite3.connect(app.config["DATABASE"]) as raw:
        assert raw.execute(
            "SELECT COUNT(*) FROM crane_count_stats").fetchone()[0] >= 1


def test_handover_input_validation(loaded_client):
    client = loaded_client
    r = _issue(client, "P1").get_json()
    serial = r["event_id"]
    base = f"/api/handover?crane_id=C1&start_serial={serial}&end_serial={serial}&limit=0"
    assert client.get(base).status_code == 200
    assert client.get(
        f"/api/handover?crane_id=C1&start_serial={serial}&end_serial={serial}&limit=-1"
    ).status_code == 400
    assert client.get(
        f"/api/handover?crane_id=C1&start_serial={serial + 1}&end_serial={serial}&limit=0"
    ).status_code == 400
    assert client.get(
        f"/api/handover?crane_id=NOPE&start_serial={serial}&end_serial={serial}&limit=0"
    ).status_code == 404
    assert client.get(
        f"/api/handover?crane_id=C1&start_serial={serial}&end_serial=999999&limit=0"
    ).status_code == 400
    assert client.get(
        f"/api/handover?crane_id=C1&start_serial=x&end_serial={serial}&limit=0"
    ).status_code == 400


def test_handover_page_shows_read_count(loaded_client):
    client = loaded_client
    r = _issue(client, "P1").get_json()
    page = client.get(
        f"/handover?crane_id=C1&start_serial={r['event_id']}"
        f"&end_serial={r['event_id']}&limit=0"
    ).data.decode()
    assert "读取" in page and "条" in page
    assert "首次超过上限" in page


def test_legacy_volume_backfills_on_startup(tmp_path):
    """模拟旧版 SQLite 卷：只有旧表、没有统计新表，启动应用后自动补齐。"""
    db_path = str(tmp_path / "legacy.sqlite3")
    # 用第一个应用实例正常导入并签发，然后删掉统计痕迹模拟旧卷
    from app import create_app
    app = create_app(database=db_path)
    with app.test_client() as client:
        upload(client, standard_peaks(), standard_work(), standard_clocks())
        client.post("/api/forms", json={"peak_id": "P1"})
        client.post("/api/forms", json={"peak_id": "P7"})
    # 模拟旧卷：删新表与标记，保留 events/forms
    with sqlite3.connect(db_path) as raw:
        raw.execute("DROP TABLE crane_count_stats")
        raw.execute("DROP TABLE app_meta")
        legacy_events = raw.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert legacy_events > 0

    # 重新启动应用：init_db 建新表并触发回填
    app2 = create_app(database=db_path)
    with app2.app_context():
        from app.db import get_db
        from app import stats
        conn = get_db()
        assert not stats.backfill_needed(conn)
        stat_rows = conn.execute(
            "SELECT COUNT(*) FROM crane_count_stats").fetchone()[0]
        assert stat_rows > 0
        # 回填后的计数与 forms 事实一致：C1 两张有效单
        reader = stats.StatReader(conn, "C1")
        max_event = conn.execute("SELECT MAX(id) FROM events").fetchone()[0]
        assert reader.prefix_count(max_event) == 2
    # 接口可直接核算，且读取条数受控
    with app2.test_client() as client:
        report = client.get(
            f"/api/handover?crane_id=C1&start_serial=1&end_serial={max_event}&limit=1"
        ).get_json()
        assert report["max_count"] == 2
        assert report["read_rows"] <= 64
