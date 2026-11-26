"""HTTP 路由：页面 + JSON API。"""
import json

from flask import (
    Blueprint,
    abort,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from . import ingest, solver, stats
from .db import get_db, is_seed_loaded

bp = Blueprint("main", __name__)


@bp.app_errorhandler(413)
def too_large(exc):
    if request.path.startswith("/api/"):
        return jsonify(error="上传文件超过 256MB 限制"), 413
    return render_template("upload.html", loaded=False,
                           error="上传文件超过 256MB 限制", result=None), 413


@bp.app_errorhandler(409)
def conflict(exc):
    if request.path.startswith("/api/"):
        return jsonify(error=exc.description), 409
    return f"<h1>409</h1><p>{exc.description}</p>", 409


@bp.app_errorhandler(500)
def internal_error(exc):
    # 任何写库中途失败：显式回滚，保证钟差、单据、统计都照旧
    conn = g.pop("db", None)
    if conn is not None:
        conn.rollback()
        conn.close()
    if request.path.startswith("/api/"):
        return jsonify(error="服务器内部错误，本笔业务已回滚，库内数据保持原样"), 500
    return "<h1>500</h1><p>服务器内部错误，本笔业务已回滚，库内数据保持原样。</p>", 500


# --------------------------------------------------------------------------
# 页面
# --------------------------------------------------------------------------
@bp.route("/")
def index():
    conn = get_db()
    loaded = is_seed_loaded(conn)
    stats = None
    if loaded:
        stats = {
            "peaks": conn.execute("SELECT COUNT(*) FROM peaks").fetchone()[0],
            "intervals": conn.execute(
                "SELECT COUNT(*) FROM work_intervals").fetchone()[0],
            "cranes": solver.list_cranes(conn),
            "clock_active": conn.execute(
                "SELECT COUNT(*) FROM clock_records WHERE active = 1").fetchone()[0],
            "forms_active": conn.execute(
                "SELECT COUNT(*) FROM forms WHERE status = 'active'").fetchone()[0],
        }
    return render_template("index.html", loaded=loaded, stats=stats)


@bp.route("/upload", methods=["GET", "POST"])
def upload_page():
    conn = get_db()
    if request.method == "GET":
        return render_template("upload.html",
                               loaded=is_seed_loaded(conn), error=None, result=None)
    try:
        result = ingest.load_season(
            conn,
            request.files.get("peaks"),
            request.files.get("work"),
            request.files.get("clocks"),
        )
        conn.commit()
    except solver.IngestError as exc:
        conn.rollback()
        return render_template("upload.html", loaded=False, error=str(exc),
                               result=None), 400
    return render_template("upload.html", loaded=True, error=None, result=result)


@bp.route("/peaks")
def peaks_page():
    conn = get_db()
    if not is_seed_loaded(conn):
        return redirect(url_for("main.upload_page"))
    q = request.args.get("q", "").strip()
    peaks = conn.execute(
        "SELECT peak_id, peak_sec FROM peaks ORDER BY peak_sec, peak_id LIMIT 200"
    ).fetchall()
    return render_template("peaks.html", peaks=peaks, q=q)


@bp.route("/peaks/<peak_id>")
def peak_detail_page(peak_id):
    conn = get_db()
    if not is_seed_loaded(conn):
        return redirect(url_for("main.upload_page"))
    peak = conn.execute("SELECT * FROM peaks WHERE peak_id = ?",
                        (peak_id,)).fetchone()
    if peak is None:
        abort(404, description=f"峰号 {peak_id} 不存在")
    determination, conflict = _determinate_or_conflict(conn, peak)
    forms = conn.execute(
        "SELECT * FROM forms WHERE peak_id = ? ORDER BY created_event",
        (peak_id,)).fetchall()
    return render_template("peak_detail.html", peak=peak,
                           determination=determination, conflict=conflict,
                           forms=forms)


@bp.route("/clocks")
def clocks_page():
    conn = get_db()
    if not is_seed_loaded(conn):
        return redirect(url_for("main.upload_page"))
    records = conn.execute(
        "SELECT * FROM clock_records ORDER BY rec_id").fetchall()
    return render_template("clocks.html", records=records)


@bp.route("/forms")
def forms_page():
    conn = get_db()
    if not is_seed_loaded(conn):
        return redirect(url_for("main.upload_page"))
    status = request.args.get("status", "")
    sql = "SELECT * FROM forms"
    params = ()
    if status in ("active", "revoked"):
        sql += " WHERE status = ?"
        params = (status,)
    sql += " ORDER BY created_event"
    forms = conn.execute(sql, params).fetchall()
    return render_template("forms.html", forms=forms, status=status)


@bp.route("/forms/<form_no>")
def form_detail_page(form_no):
    conn = get_db()
    form = conn.execute("SELECT * FROM forms WHERE form_no = ?",
                        (form_no,)).fetchone()
    if form is None:
        abort(404, description=f"复核单 {form_no} 不存在")
    peak = conn.execute("SELECT * FROM peaks WHERE peak_id = ?",
                        (form["peak_id"],)).fetchone()
    recheck = None
    if is_seed_loaded(conn):
        determination, conflict = _determinate_or_conflict(conn, peak)
        recheck = {"determination": determination, "conflict": conflict}
    events = conn.execute(
        "SELECT * FROM events WHERE id IN (?, ?) ORDER BY id",
        (form["created_event"], form["revoked_event"])).fetchall()
    return render_template("form_detail.html", form=form, peak=peak,
                           recheck=recheck, events=events)


@bp.route("/events")
def events_page():
    conn = get_db()
    events = conn.execute(
        "SELECT * FROM events ORDER BY id DESC LIMIT 300").fetchall()
    parsed = [(e["id"], e["kind"], json.loads(e["detail"])) for e in events]
    return render_template("events.html", events=parsed)


@bp.route("/handover")
def handover_page():
    """班组交接：区间内同时有效复核单峰值与首次超限流水。"""
    conn = get_db()
    if not is_seed_loaded(conn):
        return redirect(url_for("main.upload_page"))
    cranes = solver.list_cranes(conn)
    max_serial = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]

    form = {"crane_id": request.args.get("crane_id", ""),
            "start_serial": request.args.get("start_serial", ""),
            "end_serial": request.args.get("end_serial", ""),
            "limit": request.args.get("limit", "")}
    error, report = None, None
    if any(form.values()):
        try:
            crane_id = form["crane_id"].strip()
            start_serial = int(form["start_serial"])
            end_serial = int(form["end_serial"])
            limit = int(form["limit"])
        except (TypeError, ValueError):
            error = "起止流水号与上限都必须是整数。"
        else:
            if crane_id not in cranes:
                error = f"吊机班组 {crane_id} 不存在。"
            elif limit < 0:
                error = "上限必须是非负整数。"
            elif start_serial < 1 or end_serial < 1:
                error = "流水号必须是正整数。"
            elif end_serial < start_serial:
                error = "止号不能小于起号。"
            elif end_serial > max_serial:
                error = f"止号超出当前最大流水号 {max_serial}。"
            else:
                report = stats.handover_report(
                    conn, crane_id, start_serial, end_serial, limit)
                report["exceeded"] = report["first_exceed_serial"] is not None
    return render_template("handover.html", cranes=cranes, max_serial=max_serial,
                           form=form, error=error, report=report)


# --------------------------------------------------------------------------
# JSON API
# --------------------------------------------------------------------------
@bp.get("/api/health")
def api_health():
    return jsonify(ok=True, loaded=is_seed_loaded(get_db()))


@bp.get("/api/peaks/<peak_id>")
def api_peak(peak_id):
    """按峰号查询当前归属判定。读取量只与该峰/吊机相关，不随全季区间数增长。"""
    conn = get_db()
    _require_loaded(conn)
    peak = conn.execute("SELECT * FROM peaks WHERE peak_id = ?",
                        (peak_id,)).fetchone()
    if peak is None:
        return jsonify(error=f"峰号 {peak_id} 不存在"), 404
    determination, conflict = _determinate_or_conflict(conn, peak)
    active_form = conn.execute(
        "SELECT form_no FROM forms WHERE peak_id = ? AND status = 'active'",
        (peak_id,)).fetchone()
    return jsonify(
        peak_id=peak_id,
        peak_sec=peak["peak_sec"],
        determination=determination,
        conflict_records=conflict,
        active_form=active_form["form_no"] if active_form else None,
    )


@bp.post("/api/forms")
def api_issue_form():
    """对峰号签发复核单；待核峰拒绝签发。"""
    data = request.get_json(silent=True) or {}
    peak_id = (data.get("peak_id") or "").strip()
    if not peak_id:
        return jsonify(error="缺少 peak_id"), 400
    conn = get_db()
    _require_loaded(conn)
    peak = conn.execute("SELECT * FROM peaks WHERE peak_id = ?",
                        (peak_id,)).fetchone()
    if peak is None:
        return jsonify(error=f"峰号 {peak_id} 不存在"), 404

    bounds, conflict = solver.current_bounds(conn)
    if conflict is not None:
        return jsonify(
            error="当前钟差无解，不能签发复核单",
            conflict_records=conflict,
        ), 409
    determination = solver.determine_peak(conn, peak, solver.list_cranes(conn), bounds)
    if determination["result"] != "issue":
        return jsonify(error="该峰为待核峰，不能签发",
                       reason=determination["reason"],
                       windows=determination["windows"]), 409

    existing = conn.execute(
        "SELECT form_no FROM forms WHERE peak_id = ? AND status = 'active'",
        (peak_id,)).fetchone()
    if existing:
        return jsonify(error="该峰已有有效复核单",
                       form_no=existing["form_no"]), 409

    cur = conn.execute(
        "INSERT INTO events(kind, detail) VALUES ('issue', ?)",
        (json.dumps({"peak_id": peak_id, "crane_id": determination["crane"]},
                    ensure_ascii=False),))
    event_id = cur.lastrowid
    form_no = f"RC-{event_id:06d}"
    conn.execute(
        "INSERT INTO forms(form_no, peak_id, crane_id, status, created_event) "
        "VALUES (?, ?, ?, 'active', ?)",
        (form_no, peak_id, determination["crane"], event_id))
    # 统计与签发同事务入库：保存失败则事件、单据、统计一并回滚
    stats.apply_delta(conn, determination["crane"], event_id, +1)
    conn.commit()
    return jsonify(form_no=form_no, peak_id=peak_id,
                   crane_id=determination["crane"], event_id=event_id), 201


@bp.get("/api/forms/<form_no>")
def api_form_status(form_no):
    """按单号查状态（主键查询，O(1) 级）。"""
    conn = get_db()
    _require_loaded(conn)
    form = conn.execute("SELECT * FROM forms WHERE form_no = ?",
                        (form_no,)).fetchone()
    if form is None:
        return jsonify(error=f"复核单 {form_no} 不存在"), 404
    return jsonify(dict(form))


@bp.get("/api/handover")
def api_handover():
    """班组交接核算：?crane_id=&start_serial=&end_serial=&limit=。

    只读取已入库的层级汇总，返回区间内同时有效单数峰值、首次超限流水号
    以及本次核算实际读取的汇总行数（read_rows，硬上限 64）。
    """
    conn = get_db()
    _require_loaded(conn)
    try:
        crane_id = request.args["crane_id"].strip()
        start_serial = int(request.args["start_serial"])
        end_serial = int(request.args["end_serial"])
        limit = int(request.args["limit"])
    except (KeyError, TypeError, ValueError):
        return jsonify(error="需要整数参数 crane_id, start_serial, end_serial, limit"), 400
    if limit < 0:
        return jsonify(error="上限 limit 必须是非负整数"), 400
    if start_serial < 1 or end_serial < 1:
        return jsonify(error="流水号必须是正整数"), 400
    if end_serial < start_serial:
        return jsonify(error="end_serial 不能小于 start_serial"), 400
    if crane_id not in solver.list_cranes(conn):
        return jsonify(error=f"吊机班组 {crane_id} 不存在"), 404
    max_serial = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
    if end_serial > max_serial:
        return jsonify(error=f"end_serial 超出当前最大流水号 {max_serial}"), 400

    report = stats.handover_report(
        conn, crane_id, start_serial, end_serial, limit)
    report["exceeded"] = report["first_exceed_serial"] is not None
    report["max_query_reads"] = stats.MAX_QUERY_READS
    return jsonify(report)


@bp.get("/api/clock-records")
def api_clock_records():
    conn = get_db()
    _require_loaded(conn)
    rows = conn.execute(
        "SELECT * FROM clock_records ORDER BY rec_id").fetchall()
    return jsonify([dict(r) for r in rows])


@bp.post("/api/clock-records/<int:rec_id>/correct")
def api_correct_clock(rec_id):
    """逐条更正钟差。无解则返回一组冲突记录，数据与复核单原样不动。"""
    data = request.get_json(silent=True) or {}
    try:
        low = int(data["low_sec"])
        high = int(data["high_sec"])
    except (KeyError, TypeError, ValueError):
        return jsonify(error="需要整数参数 low_sec, high_sec"), 400
    if high < low:
        return jsonify(error=f"上界 {high} 小于下界 {low}，无解"), 400

    conn = get_db()
    _require_loaded(conn)
    record = conn.execute(
        "SELECT * FROM clock_records WHERE rec_id = ? AND active = 1",
        (rec_id,)).fetchone()
    if record is None:
        return jsonify(error=f"有效钟差记录 {rec_id} 不存在（可能已被更正）"), 404

    # 先在临时改写后的图上试解；发现负环直接拒绝，库内数据与复核单保持原样
    trial = [dict(r) for r in solver.active_clock_records(conn)
             if r["rec_id"] != rec_id]
    trial.append({"rec_id": rec_id, "device_a": record["device_a"],
                  "device_b": record["device_b"],
                  "low_sec": low, "high_sec": high})
    nodes, edges = solver.build_graph(trial)
    conflict = solver.find_negative_cycle(edges, nodes)
    if conflict is not None:
        return jsonify(
            error="更正后钟差无解（存在负环），更正未生效，数据与复核单保持原样",
            conflict_records=conflict,
        ), 409

    # 可行：落库更正，再按入库先后重新核定所有有效单
    cur = conn.execute(
        "INSERT INTO events(kind, detail) VALUES ('correction', ?)",
        (json.dumps({
            "rec_id": rec_id,
            "device_a": record["device_a"],
            "device_b": record["device_b"],
            "old_low": record["low_sec"], "old_high": record["high_sec"],
            "new_low": low, "new_high": high,
        }, ensure_ascii=False),))
    correction_event = cur.lastrowid

    conn.execute(
        "UPDATE clock_records SET active = 0, superseded_by = ? WHERE rec_id = ?",
        (correction_event, rec_id))
    conn.execute(
        "INSERT INTO clock_records(rec_id, device_a, device_b, low_sec, high_sec, "
        "active, created_event) VALUES (?, ?, ?, ?, ?, 1, ?)",
        (rec_id, record["device_a"], record["device_b"], low, high,
         correction_event))

    bounds, conflict2 = solver.current_bounds(conn)
    # 已做过负环试解，这里仅可能因连通性变化产生 None，按失归属处理
    revoked = []
    revoked_per_crane = {}
    active_forms = conn.execute(
        "SELECT * FROM forms WHERE status = 'active' ORDER BY created_event"
    ).fetchall()
    for form in active_forms:
        keep = False
        if conflict2 is None and bounds is not None:
            peak = conn.execute("SELECT * FROM peaks WHERE peak_id = ?",
                                (form["peak_id"],)).fetchone()
            det = solver.determine_peak(
                conn, peak, solver.list_cranes(conn), bounds)
            keep = det["result"] == "issue" and det["crane"] == form["crane_id"]
        if not keep:
            reason = "钟差更正后该峰不再唯一归属原班组"
            if conflict2 is not None:
                reason = f"钟差更正后系统无解，冲突记录 {conflict2}"
            rev = conn.execute(
                "INSERT INTO events(kind, detail) VALUES ('revoke', ?)",
                (json.dumps({"form_no": form["form_no"],
                             "peak_id": form["peak_id"],
                             "correction_event": correction_event,
                             "reason": reason}, ensure_ascii=False),))
            revoke_event = rev.lastrowid
            conn.execute(
                "UPDATE forms SET status = 'revoked', revoked_event = ?, "
                "revoke_reason = ? WHERE form_no = ?",
                (revoke_event, reason, form["form_no"]))
            revoked.append({"form_no": form["form_no"],
                            "peak_id": form["peak_id"],
                            "event_id": revoke_event})
            # 同次钟差更正撤回的单从更正号起一并扣除；随后的撤单流水沿用更正后的单数
            revoked_per_crane[form["crane_id"]] = \
                revoked_per_crane.get(form["crane_id"], 0) + 1

    # 统计与钟差更正、撤单同事务入库：任一保存失败则钟差、单据、统计都照旧
    for crane_id, amount in revoked_per_crane.items():
        stats.apply_delta(conn, crane_id, correction_event, -amount)

    conn.commit()
    return jsonify(
        ok=True,
        correction_event=correction_event,
        rec_id=rec_id,
        old={"low_sec": record["low_sec"], "high_sec": record["high_sec"]},
        new={"low_sec": low, "high_sec": high},
        revoked=revoked,
        revoked_count=len(revoked),
        note="撤销的旧单永久保留；即使之后改回原归属也不恢复旧单，需重新签发新单",
    )


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------
def _require_loaded(conn):
    if not is_seed_loaded(conn):
        abort(409, description="尚未导入本季 CSV 数据")


def _determinate_or_conflict(conn, peak):
    """返回 (determination_or_None, conflict_rec_ids_or_None)。"""
    bounds, conflict = solver.current_bounds(conn)
    if conflict is not None:
        return None, conflict
    return solver.determine_peak(conn, peak, solver.list_cranes(conn), bounds), None
