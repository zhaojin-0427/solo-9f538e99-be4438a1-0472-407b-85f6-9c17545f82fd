"""网页路由：上传整份修订、逐轮计算、当前结果与历史查询。

关键约束：
- 生成结果固定两份输入修订（run.obs_revision_id / run.tax_revision_id）；
- 任一输入整份更正后，旧的当前结果立即失效（同事务清除 is_current），但保留可查；
- 计算完成时若固定的修订已非最新，按迟到计算处理，仅留作历史；
- 运行记录与其全部结果行在同一事务写入，失败整体回滚，只留一条无结果的失败记录。
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from flask import (Blueprint, Response, abort, flash, redirect,
                   render_template, request, url_for)

from . import taxonomy
from .db import get_db

bp = Blueprint("main", __name__)

_KIND_CONF = {
    "obs": ("obs_revision", "观测"),
    "tax": ("tax_revision", "分类沿革"),
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


@bp.route("/")
def index():
    db = get_db()
    obs = db.execute(
        "SELECT * FROM obs_revision ORDER BY id DESC LIMIT 1").fetchone()
    tax = db.execute(
        "SELECT * FROM tax_revision ORDER BY id DESC LIMIT 1").fetchone()
    current = db.execute(
        "SELECT * FROM run WHERE is_current = 1 AND status = 'ok'"
        " ORDER BY id DESC LIMIT 1").fetchone()
    usable = pending = []
    if current is not None:
        usable = db.execute(
            "SELECT * FROM run_usable WHERE run_id = ? ORDER BY record_no",
            (current["id"],)).fetchall()
        pending = db.execute(
            "SELECT * FROM run_pending WHERE run_id = ? ORDER BY record_no",
            (current["id"],)).fetchall()
    obs_revs = db.execute("SELECT * FROM obs_revision ORDER BY id DESC").fetchall()
    tax_revs = db.execute("SELECT * FROM tax_revision ORDER BY id DESC").fetchall()
    return render_template("index.html", obs=obs, tax=tax, current=current,
                           usable=usable, pending=pending,
                           obs_revs=obs_revs, tax_revs=tax_revs)


def _parse_upload(kind: str, text: str) -> int:
    """校验上传内容，返回数据行数；失败抛 CsvError。"""
    if kind == "obs":
        return len(taxonomy.parse_observations(text))
    return taxonomy.parse_taxonomy(text)[2]


def _handle_upload(kind: str):
    table, label = _KIND_CONF[kind]
    if request.method == "POST":
        data = b""
        filename = ""
        file = request.files.get("file")
        if file is not None and file.filename:
            data = file.read()
            filename = file.filename
        elif request.form.get("pasted", "").strip():
            data = request.form["pasted"].encode("utf-8")
            filename = "（粘贴文本）"
        if not data:
            flash(f"请选择{label} CSV 文件，或粘贴 CSV 文本", "error")
            return redirect(request.url)
        try:
            text = taxonomy.decode_csv_bytes(data)
            row_count = _parse_upload(kind, text)
        except taxonomy.CsvError as exc:
            for line in str(exc).splitlines():
                flash(line, "error")
            flash(f"{label} CSV 未通过校验，本次更正未保存", "error")
            return redirect(request.url)
        digest = hashlib.sha256(data).hexdigest()
        db = get_db()
        db.execute("BEGIN IMMEDIATE")
        try:
            cur = db.execute(
                f"INSERT INTO {table}"
                " (uploaded_at, filename, sha256, row_count, content)"
                " VALUES (?, ?, ?, ?, ?)",
                (_now(), filename, digest, row_count, text))
            rev_id = cur.lastrowid
            # 输入已整份更正：旧的当前结果立即失效（仍保留可查）
            cleared = db.execute(
                "UPDATE run SET is_current = 0 WHERE is_current = 1").rowcount
            db.commit()
        except Exception:
            db.rollback()
            raise
        msg = (f"{label} CSV 已整份保存为修订 #{rev_id}"
               f"（{row_count} 行，旧修订保留可查）")
        if cleared:
            msg += "；输入已更正，此前的当前结果已失效，请重新计算"
        flash(msg, "ok")
        return redirect(url_for("main.index"))
    return render_template("upload.html", kind=kind, label=label)


@bp.route("/upload/observations", methods=["GET", "POST"])
def upload_obs():
    return _handle_upload("obs")


@bp.route("/upload/taxonomy", methods=["GET", "POST"])
def upload_tax():
    return _handle_upload("tax")


@bp.route("/compute", methods=["POST"])
def compute():
    db = get_db()
    try:
        obs_id = int(request.form.get("obs_revision_id", ""))
        tax_id = int(request.form.get("tax_revision_id", ""))
    except ValueError:
        flash("请选择要固定的观测修订与沿革修订", "error")
        return redirect(url_for("main.index"))
    obs = db.execute("SELECT * FROM obs_revision WHERE id = ?", (obs_id,)).fetchone()
    tax = db.execute("SELECT * FROM tax_revision WHERE id = ?", (tax_id,)).fetchone()
    if obs is None or tax is None:
        flash("所选输入修订不存在", "error")
        return redirect(url_for("main.index"))
    try:
        obs_rows = taxonomy.parse_observations(obs["content"])
        rounds_map, max_round, _ = taxonomy.parse_taxonomy(tax["content"])
        usable, pending = taxonomy.compute_results(obs_rows, rounds_map, max_round)
        run_id, is_current = _write_run(db, obs_id, tax_id, usable, pending)
    except taxonomy.CsvError as exc:
        _record_failed_run(db, obs_id, tax_id, f"输入解析失败：{exc}")
        flash("计算失败：固定修订的内容无法解析，未产生任何结果", "error")
        return redirect(url_for("main.runs"))
    except Exception as exc:  # 任何失败都不得开放半份结果
        _record_failed_run(db, obs_id, tax_id, f"{type(exc).__name__}: {exc}")
        flash("计算失败，未开放任何部分结果；失败记录已留档", "error")
        return redirect(url_for("main.runs"))
    if is_current:
        flash(f"计算完成：可用 {len(usable)} 条、待核 {len(pending)} 条，"
              f"已作为当前结果（运行 #{run_id}）", "ok")
    else:
        flash(f"计算完成：可用 {len(usable)} 条、待核 {len(pending)} 条；"
              f"固定的并非最新修订，按迟到计算仅留作历史（运行 #{run_id}）", "ok")
    return redirect(url_for("main.run_detail", run_id=run_id))


def _write_run(db, obs_id: int, tax_id: int, usable: list, pending: list):
    """单事务写入运行与全部结果，返回 (run_id, is_current)。

    是否成为当前结果以提交时刻的最新修订为准：若固定的修订已非最新
    （例如计算期间输入被更正），则按迟到计算仅留作历史。
    """
    db.execute("BEGIN IMMEDIATE")
    try:
        latest_obs = db.execute("SELECT MAX(id) AS m FROM obs_revision").fetchone()["m"]
        latest_tax = db.execute("SELECT MAX(id) AS m FROM tax_revision").fetchone()["m"]
        is_current = 1 if (obs_id == latest_obs and tax_id == latest_tax) else 0
        note = "" if is_current else "固定的输入修订并非最新，按迟到计算处理，仅留作历史"
        if is_current:
            db.execute("UPDATE run SET is_current = 0 WHERE is_current = 1")
        cur = db.execute(
            "INSERT INTO run (created_at, obs_revision_id, tax_revision_id, status,"
            " is_current, note, usable_count, pending_count)"
            " VALUES (?, ?, ?, 'ok', ?, ?, ?, ?)",
            (_now(), obs_id, tax_id, is_current, note, len(usable), len(pending)))
        run_id = cur.lastrowid
        db.executemany(
            "INSERT INTO run_usable"
            " (run_id, record_no, initial_taxon, final_taxon, quantity)"
            " VALUES (?, ?, ?, ?, ?)",
            [(run_id, r["record_no"], r["initial_taxon"], r["final_taxon"],
              r["quantity"]) for r in usable])
        db.executemany(
            "INSERT INTO run_pending"
            " (run_id, record_no, initial_taxon, quantity, final_taxons, reason)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [(run_id, r["record_no"], r["initial_taxon"], r["quantity"],
              r["final_taxons"], r["reason"]) for r in pending])
        db.commit()
        return run_id, is_current
    except Exception:
        db.rollback()
        raise


def _record_failed_run(db, obs_id: int, tax_id: int, error: str) -> None:
    """失败只留一条无结果行的运行记录，绝不写入部分结果。"""
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT INTO run (created_at, obs_revision_id, tax_revision_id, status,"
            " is_current, note, error)"
            " VALUES (?, ?, ?, 'failed', 0, '失败，未产生任何结果', ?)",
            (_now(), obs_id, tax_id, error))
        db.commit()
    except Exception:
        db.rollback()


@bp.route("/runs")
def runs():
    rows = get_db().execute("SELECT * FROM run ORDER BY id DESC").fetchall()
    return render_template("runs.html", runs=rows)


@bp.route("/runs/<int:run_id>")
def run_detail(run_id: int):
    db = get_db()
    run = db.execute("SELECT * FROM run WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        abort(404)
    usable = db.execute(
        "SELECT * FROM run_usable WHERE run_id = ? ORDER BY record_no",
        (run_id,)).fetchall()
    pending = db.execute(
        "SELECT * FROM run_pending WHERE run_id = ? ORDER BY record_no",
        (run_id,)).fetchall()
    latest_obs = db.execute("SELECT MAX(id) AS m FROM obs_revision").fetchone()["m"]
    latest_tax = db.execute("SELECT MAX(id) AS m FROM tax_revision").fetchone()["m"]
    return render_template("run_detail.html", run=run, usable=usable,
                           pending=pending,
                           latest_obs=latest_obs, latest_tax=latest_tax)


@bp.route("/revisions")
def revisions():
    db = get_db()
    obs_revs = db.execute("SELECT * FROM obs_revision ORDER BY id DESC").fetchall()
    tax_revs = db.execute("SELECT * FROM tax_revision ORDER BY id DESC").fetchall()
    return render_template("revisions.html", obs_revs=obs_revs, tax_revs=tax_revs)


@bp.route("/revisions/<kind>/<int:rev_id>/download")
def download_revision(kind: str, rev_id: int):
    if kind not in _KIND_CONF:
        abort(404)
    table, _ = _KIND_CONF[kind]
    row = get_db().execute(
        f"SELECT * FROM {table} WHERE id = ?", (rev_id,)).fetchone()
    if row is None:
        abort(404)
    return Response(
        row["content"], mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f"attachment; filename={kind}_revision_{rev_id}.csv"})
