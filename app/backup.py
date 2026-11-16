"""整库备份的导出、校验与恢复。

备份格式（ZIP，仅接受本应用导出的完整备份）：
- APP_BACKUP 标识文件：魔数 + 格式版本 + 导出时间；
- manifest.json：每张表的行数、列名与数据 SHA-256、备份整体 HMAC-SHA256，
  HMAC 密钥取自应用 SECRET_KEY，用于确认备份确实由本应用导出且未被篡改；
- data/ 下每张表一个 JSON 文件，整表数据连同原始行号（id）一起保存，
  恢复时显式写回原 id，从而保留修订号、运行号与 is_current 当前结果标记。

安全 / 完整性约定：
- 校验全部通过后才允许进入写事务；任何损坏、缺件、引用缺失、签名不符
  都在写库前拒绝，原库保持不变；
- 恢复在单事务内完成（清空业务表 → 按原 id 写回 → 递增 generation），
  事务中途失败整体回滚，只可能保留恢复前或恢复后的完整状态；
- generation 递增构成并发围栏：恢复开始前已打开的上传 / 计算写事务
  在 begin_write 处复查失败并回滚，不会把旧库的修订写回新库。
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import secrets
import zipfile
from datetime import datetime, timezone

from .db import BACKUP_TABLES

BACKUP_MAGIC = "BIODIVERSITY-TAXONOMY-APP-BACKUP"
BACKUP_FORMAT_VERSION = 1
BACKUP_MARKER = "APP_BACKUP"
MANIFEST_NAME = "manifest.json"
DATA_DIR = "data"

# 备份文件大小上限：与 Flask MAX_CONTENT_LENGTH（1 MiB）保持一致，
# 正常数据（≤100 行观测、≤40 行沿革、少量运行）的压缩备份远小于此，仅用于防御。
MAX_BACKUP_BYTES = 1024 * 1024


class BackupError(Exception):
    """备份损坏 / 非本应用导出 / 引用缺失等；消息面向用户。"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _table_columns(db, table: str) -> list[str]:
    return [row["name"] for row in db.execute(
        f"PRAGMA table_info({table})").fetchall()]


def _dump_table(db, table: str) -> list[dict]:
    """整表导出为字典列表（含 id，恢复时按原 id 写回）。"""
    columns = _table_columns(db, table)
    order = " ORDER BY id" if "id" in columns else ""
    rows = db.execute(f"SELECT * FROM {table}{order}").fetchall()
    return [dict(r) for r in rows]


def _canonical_bytes(payload) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")


def create_backup(db, secret_key: str) -> bytes:
    """在当前快照事务内导出整库，返回 ZIP 字节串。"""
    tables: dict[str, list[dict]] = {}
    manifest_tables: dict[str, dict] = {}
    for table in BACKUP_TABLES:
        payload = _dump_table(db, table)
        tables[table] = payload
        raw = _canonical_bytes(payload)
        manifest_tables[table] = {
            "columns": _table_columns(db, table),
            "row_count": len(payload),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    marker = {
        "magic": BACKUP_MAGIC,
        "format_version": BACKUP_FORMAT_VERSION,
        "exported_at": _now(),
        "tables": list(BACKUP_TABLES),
    }
    manifest = {
        "format_version": BACKUP_FORMAT_VERSION,
        "exported_at": marker["exported_at"],
        "tables": manifest_tables,
    }
    # HMAC 覆盖标识文件与全部表数据，防止伪造 / 篡改后冒充本应用备份
    mac = hmac.new(
        str(secret_key).encode("utf-8"), digestmod=hashlib.sha256)
    mac.update(_canonical_bytes(marker))
    for table in BACKUP_TABLES:
        mac.update(_canonical_bytes(tables[table]))
    manifest["hmac_sha256"] = mac.hexdigest()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(BACKUP_MARKER, _canonical_bytes(marker))
        zf.writestr(MANIFEST_NAME, json.dumps(
            manifest, ensure_ascii=False, indent=2))
        for table in BACKUP_TABLES:
            zf.writestr(f"{DATA_DIR}/{table}.json",
                        json.dumps(tables[table], ensure_ascii=False, indent=1))
    return buf.getvalue()


def _read_zip(data: bytes) -> dict[str, bytes]:
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise BackupError("文件不是有效的备份包（无法识别为 ZIP 归档），已拒绝恢复")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            bad = zf.testzip()
            if bad is not None:
                raise BackupError(f"备份包内部文件损坏（{bad}），已拒绝恢复")
            names = zf.namelist()
            return {name: zf.read(name) for name in names}
    except zipfile.BadZipFile as exc:
        raise BackupError(f"备份包已损坏：{exc}，已拒绝恢复") from exc
    except RuntimeError as exc:  # 加密 / 不支持的压缩方式等
        raise BackupError(f"备份包无法读取：{exc}，已拒绝恢复") from exc


def _load_json(raw: bytes, label: str):
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError(f"备份中的 {label} 已损坏或不是合法 JSON，已拒绝恢复") from exc


def parse_and_validate_backup(data: bytes, secret_key: str) -> dict:
    """完整校验备份，返回 {tables: {表: [行...]}, manifest, marker}。

    只读不写库：任何失败都抛 BackupError，原库不变。
    """
    if not data:
        raise BackupError("备份文件为空，已拒绝恢复")
    if len(data) > MAX_BACKUP_BYTES:
        raise BackupError("备份文件超出允许大小，已拒绝恢复")
    files = _read_zip(data)

    if BACKUP_MARKER not in files:
        raise BackupError(
            f"备份缺少 {BACKUP_MARKER} 标识文件，不是本应用导出的完整备份，已拒绝恢复")
    if MANIFEST_NAME not in files:
        raise BackupError("备份缺少 manifest.json，不是本应用导出的完整备份，已拒绝恢复")

    marker = _load_json(files[BACKUP_MARKER], BACKUP_MARKER)
    manifest = _load_json(files[MANIFEST_NAME], "manifest.json")
    if not isinstance(marker, dict) or not isinstance(manifest, dict):
        raise BackupError("备份元数据结构非法，已拒绝恢复")
    if marker.get("magic") != BACKUP_MAGIC:
        raise BackupError("备份魔数不匹配，不是本应用导出的备份，已拒绝恢复")
    if marker.get("format_version") != BACKUP_FORMAT_VERSION or \
            manifest.get("format_version") != BACKUP_FORMAT_VERSION:
        raise BackupError(
            f"备份格式版本不受支持（本应用支持版本 {BACKUP_FORMAT_VERSION}），已拒绝恢复")
    if marker.get("tables") != list(BACKUP_TABLES):
        raise BackupError("备份的表清单与本应用不一致，不是完整备份，已拒绝恢复")

    expected_tables = set(BACKUP_TABLES)
    if set(manifest.get("tables", {}).keys()) != expected_tables:
        raise BackupError("manifest 中的表清单不完整，已拒绝恢复")

    tables: dict[str, list] = {}
    for table in BACKUP_TABLES:
        name = f"{DATA_DIR}/{table}.json"
        if name not in files:
            raise BackupError(f"备份缺少数据文件 {name}，不是完整备份，已拒绝恢复")
        payload = _load_json(files[name], name)
        if not isinstance(payload, list):
            raise BackupError(f"备份中 {table} 的数据结构非法，已拒绝恢复")
        meta = manifest["tables"][table]
        raw = _canonical_bytes(payload)
        if hashlib.sha256(raw).hexdigest() != meta.get("sha256"):
            raise BackupError(
                f"备份中 {table} 的内容与校验和不符（文件被改动或损坏），已拒绝恢复")
        if len(payload) != meta.get("row_count"):
            raise BackupError(f"备份中 {table} 的行数与清单不符，已拒绝恢复")
        tables[table] = payload

    # 校验 HMAC：确认备份由本应用（持有相同 SECRET_KEY）导出且未被篡改
    mac = hmac.new(
        str(secret_key).encode("utf-8"), digestmod=hashlib.sha256)
    mac.update(_canonical_bytes(marker))
    for table in BACKUP_TABLES:
        mac.update(_canonical_bytes(tables[table]))
    if not hmac.compare_digest(mac.hexdigest(), manifest.get("hmac_sha256", "")):
        raise BackupError(
            "备份签名（HMAC）校验失败：不是本应用导出的备份或内容已被篡改，已拒绝恢复")

    _validate_referential_integrity(tables)
    return {"tables": tables, "manifest": manifest, "marker": marker}


def _validate_referential_integrity(tables: dict[str, list]) -> None:
    """结构与引用完整性检查；任何问题都在写库前拒绝。"""
    obs_ids, tax_ids, run_ids = set(), set(), set()
    for rev_id in _iter_ids(tables["obs_revision"]):
        if rev_id in obs_ids:
            raise BackupError("备份中观测修订存在重复编号，已拒绝恢复")
        obs_ids.add(rev_id)
    for rev_id in _iter_ids(tables["tax_revision"]):
        if rev_id in tax_ids:
            raise BackupError("备份中沿革修订存在重复编号，已拒绝恢复")
        tax_ids.add(rev_id)
    for run_id in _iter_ids(tables["run"]):
        if run_id in run_ids:
            raise BackupError("备份中计算记录存在重复编号，已拒绝恢复")
        run_ids.add(run_id)
    if not obs_ids or not tax_ids:
        raise BackupError("完整备份必须包含至少一份观测修订与沿革修订，已拒绝恢复")

    for row in tables["run"]:
        if row.get("obs_revision_id") not in obs_ids:
            raise BackupError(
                f"计算记录 #{row.get('id')} 引用了不存在的观测修订 "
                f"#{row.get('obs_revision_id')}，引用缺失，已拒绝恢复")
        if row.get("tax_revision_id") not in tax_ids:
            raise BackupError(
                f"计算记录 #{row.get('id')} 引用了不存在的沿革修订 "
                f"#{row.get('tax_revision_id')}，引用缺失，已拒绝恢复")
        if row.get("status") not in ("ok", "failed"):
            raise BackupError("备份中存在非法的计算状态，已拒绝恢复")

    current_runs = [r["id"] for r in tables["run"] if r.get("is_current")]
    if len(current_runs) > 1:
        raise BackupError("备份中存在多条当前结果标记，数据不一致，已拒绝恢复")
    if current_runs:
        current = next(r for r in tables["run"] if r["id"] == current_runs[0])
        if current["status"] != "ok":
            raise BackupError("备份把失败记录标记为当前结果，数据不一致，已拒绝恢复")

    for table, label in (("run_usable", "可用结果"), ("run_pending", "待核结果")):
        for row in tables[table]:
            if row.get("run_id") not in run_ids:
                raise BackupError(
                    f"{label}行引用了不存在的计算记录 #{row.get('run_id')}，"
                    "引用缺失，已拒绝恢复")

    # 结果行数与运行汇总一致
    usable_per_run: dict[int, int] = {}
    pending_per_run: dict[int, int] = {}
    for row in tables["run_usable"]:
        usable_per_run[row["run_id"]] = usable_per_run.get(row["run_id"], 0) + 1
    for row in tables["run_pending"]:
        pending_per_run[row["run_id"]] = pending_per_run.get(row["run_id"], 0) + 1
    for row in tables["run"]:
        rid = row["id"]
        if row["status"] == "ok":
            if usable_per_run.get(rid, 0) != row.get("usable_count") or \
                    pending_per_run.get(rid, 0) != row.get("pending_count"):
                raise BackupError(
                    f"计算记录 #{rid} 的结果行数与汇总数不符，数据不一致，已拒绝恢复")
        elif rid in usable_per_run or rid in pending_per_run:
            raise BackupError(
                f"失败的计算记录 #{rid} 不允许携带结果行，数据不一致，已拒绝恢复")


def _iter_ids(rows: list):
    for row in rows:
        rid = row.get("id")
        if not isinstance(rid, int) or isinstance(rid, bool) or rid <= 0:
            raise BackupError("备份中存在非法或缺失的编号，已拒绝恢复")
        yield rid


# 删除顺序：子表在前、父表在后；插入顺序取反即满足外键约束
_DELETE_ORDER = ("run_usable", "run_pending", "run",
                 "obs_revision", "tax_revision")
_INSERT_ORDER = tuple(reversed(_DELETE_ORDER))


def restore_backup(db, parsed: dict) -> None:
    """单事务整库替换：清空业务表 → 按原 id 写回 → 递增 generation。

    调用方负责先通过 begin_write 拿到写锁并通过 generation 围栏；
    本函数内任何异常都向上抛出，由调用方整体回滚。
    外键开关无法在事务内变更，因此按依赖顺序删 / 插。
    """
    tables = parsed["tables"]
    for table in _DELETE_ORDER:
        db.execute(f"DELETE FROM {table}")
    for table in _INSERT_ORDER:  # 父表先插入，引用自然成立
        rows = tables[table]
        if not rows:
            continue
        columns = list(rows[0].keys())
        placeholders = ",".join("?" for _ in columns)
        collist = ",".join(columns)
        db.executemany(
            f"INSERT INTO {table} ({collist}) VALUES ({placeholders})",
            [[row[col] for col in columns] for row in rows])
    # 同步 AUTOINCREMENT 序列，恢复后新修订继续在最大编号之后递增。
    # sqlite_sequence 是无唯一约束的内部表，不能用 ON CONFLICT 更新，
    # 必须先删该行再插入（在同一事务内，回滚可整体撤销）。
    # run_usable / run_pending 无自增 id，跳过。
    for table in BACKUP_TABLES:
        rows = tables[table]
        if not rows or "id" not in rows[0]:
            continue
        max_id = max(r["id"] for r in rows)
        if max_id:
            db.execute(
                "DELETE FROM sqlite_sequence WHERE name = ?", (table,))
            db.execute(
                "INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)",
                (table, max_id))
    # 围栏版本号递增：恢复前已开始的其它写事务提交前复查将失败
    db.execute(
        "UPDATE app_state SET generation = generation + 1 WHERE id = 1")
    # 恢复后立即核对外键完整性，有任何问题直接抛错触发回滚
    violations = db.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise BackupError(
            f"恢复后外键检查发现 {len(violations)} 处引用问题，已整体回滚")


def new_restore_token() -> str:
    """生成一次性恢复确认令牌。"""
    return secrets.token_urlsafe(24)


def backup_filename(exported_at: str) -> str:
    stamp = exported_at.replace(" ", "_").replace(":", "").replace("UTC", "").strip("_")
    return f"backup_{stamp}.zip"
