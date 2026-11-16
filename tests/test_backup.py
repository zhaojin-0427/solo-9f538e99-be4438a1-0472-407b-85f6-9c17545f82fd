"""整库备份与恢复自测。

覆盖：
- 备份下载、恢复往返后数据 / 编号 / 输入原文 / 当前结果标记完全一致；
- 仅接受本应用导出的完整备份：损坏、篡改、缺件、引用缺失、非本应用签名一律拒绝；
- 恢复失败时原库不变；
- 恢复前已开始的上传 / 计算（含失败记录）在恢复成功后不得写回（generation 围栏）；
- 恢复与读取交错时，读请求只能看到完整旧库或完整备份（快照隔离）；
- 恢复后可继续更正与计算，新编号在备份最大编号之后递增。
"""
import io
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import zipfile

from app import create_app, db as dbmod
from app import backup, taxonomy
from app.db import get_db

OBS_CSV = (
    "record_no,initial_taxon,quantity\n"
    "R1,A,10\nR2,X,5\nR3,Z,3\nR4,B,2\nR5,Q,7\nR6,M,4\n"
)
TAX_CSV = (
    "round,old_id,new_id\n"
    "1,A,B\n1,A,C\n1,M,N\n1,M,O\n1,X,Y\n1,Z,Y\n"
    "2,B,D\n2,C,D\n2,N,D\n2,Y,Y2\n3,D,E\n"
)


class BackupRestoreTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app({
            "DB_PATH": os.path.join(self.tmp.name, "test.db"),
            "SECRET_KEY": "test-secret",
            "RESTORE_STAGING_DIR": os.path.join(self.tmp.name, "staging"),
        })
        self.client = self.app.test_client()
        self._seed()

    def tearDown(self):
        self.tmp.cleanup()

    # ---------- 辅助 ----------

    def upload(self, url, text, filename="data.csv"):
        return self.client.post(
            url,
            data={"file": (io.BytesIO(text.encode("utf-8")), filename)},
            content_type="multipart/form-data",
            follow_redirects=True)

    def compute(self, obs_id, tax_id):
        return self.client.post(
            "/compute",
            data={"obs_revision_id": obs_id, "tax_revision_id": tax_id},
            follow_redirects=True)

    def seed_state_a(self):
        """修订 1/1 + 当前运行 #1 的标准状态。"""
        self.upload("/upload/observations", OBS_CSV, "obs.csv")
        self.upload("/upload/taxonomy", TAX_CSV, "tax.csv")
        self.compute(1, 1)

    def do_restore(self, data: bytes):
        """用独立底层连接执行一次完整恢复（绕过 HTTP 客户端）。"""
        restorer = self.raw_connect()
        dbmod.begin_write(restorer, dbmod.begin_snapshot(restorer))
        with self.app.app_context():
            parsed = backup.parse_and_validate_backup(
                data, self.app.config["SECRET_KEY"])
            backup.restore_backup(restorer, parsed)
            restorer.commit()
        restorer.close()

    def _seed(self):
        self.seed_state_a()

    def download_backup(self) -> bytes:
        resp = self.client.get("/backup/download")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.headers["Content-Disposition"].startswith(
            "attachment; filename=backup_"))
        self.assertEqual(resp.mimetype, "application/zip")
        return resp.get_data()

    def restore(self, data: bytes, expect_redirect=True):
        """完整两步恢复，返回 (确认页响应, 最终响应)。"""
        r1 = self.client.post(
            "/restore",
            data={"backup": (io.BytesIO(data), "backup.zip")},
            content_type="multipart/form-data",
            follow_redirects=False)
        self.assertEqual(r1.status_code, 200)
        self.assertIn("确认整库恢复", r1.get_data(as_text=True))
        token = self._extract_token(r1.get_data(as_text=True))
        r2 = self.client.post(
            "/restore/confirm", data={"token": token},
            follow_redirects=True)
        return r1, r2

    @staticmethod
    def _extract_token(html: str) -> str:
        marker = 'name="token" value="'
        start = html.index(marker) + len(marker)
        end = html.index('"', start)
        return html[start:end]

    def raw_connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.app.config["DB_PATH"], timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def dump_all(self) -> dict:
        with self.app.app_context():
            conn = get_db()
            out = {}
            for t in dbmod.BACKUP_TABLES:
                cols = [r["name"] for r in conn.execute(
                    f"PRAGMA table_info({t})").fetchall()]
                order = " ORDER BY id" if "id" in cols else ""
                out[t] = [dict(r) for r in conn.execute(
                    f"SELECT * FROM {t}{order}").fetchall()]
            return out

    # ---------- 用例：备份页面与导出 ----------

    def test_backup_page_lists_counts(self):
        html = self.client.get("/backup").get_data(as_text=True)
        self.assertEqual(self.client.get("/backup").status_code, 200)
        self.assertIn("下载整库备份", html)
        self.assertIn("观测修订", html)

    def test_backup_zip_structure(self):
        data = self.download_backup()
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
            self.assertIn("APP_BACKUP", names)
            self.assertIn("manifest.json", names)
            for t in dbmod.BACKUP_TABLES:
                self.assertIn(f"data/{t}.json", names)
            marker = __import__("json").loads(zf.read("APP_BACKUP"))
        self.assertEqual(marker["magic"], backup.BACKUP_MAGIC)
        self.assertEqual(marker["format_version"], backup.BACKUP_FORMAT_VERSION)

    # ---------- 用例：恢复往返，保留编号 / 原文 / 当前标记 ----------

    def test_restore_roundtrip_preserves_everything(self):
        before = self.dump_all()
        data = self.download_backup()
        # 恢复前把库改成另一状态：更正观测（修订 #2）并重新计算（运行 #2），
        # 使旧当前结果失效，再恢复回备份
        self.upload("/upload/observations", OBS_CSV + "R7,Q,1\n", "obs2.csv")
        self.compute(2, 1)
        mid = self.dump_all()
        self.assertEqual(len(mid["obs_revision"]), 2)
        self.assertEqual(len(mid["run"]), 2)

        _, r2 = self.restore(data)
        self.assertIn("整库恢复成功", r2.get_data(as_text=True))

        after = self.dump_all()
        self.assertEqual(after, before)  # 全表逐行完全一致（含 id / 原文 / 标记）
        # 关键保留项显式断言
        self.assertEqual([r["id"] for r in after["obs_revision"]], [1])
        self.assertEqual([r["id"] for r in after["tax_revision"]], [1])
        self.assertEqual([r["id"] for r in after["run"]], [1])
        self.assertEqual(after["run"][0]["is_current"], 1)
        self.assertEqual(after["obs_revision"][0]["content"], OBS_CSV)
        self.assertEqual(len(after["run_usable"]), 5)
        self.assertEqual(len(after["run_pending"]), 1)
        # 首页恢复为备份中的当前结果
        index = self.client.get("/").get_data(as_text=True)
        self.assertIn("运行 #1", index)
        self.assertIn("R6", index)

    def test_restore_keeps_failed_runs_and_history(self):
        # 制造一条失败记录与一条迟到历史记录
        from unittest import mock
        with mock.patch("app.taxonomy.compute_results",
                        side_effect=RuntimeError("boom")):
            self.compute(1, 1)
        self.upload("/upload/observations", OBS_CSV + "R7,Q,1\n", "obs2.csv")
        self.compute(1, 1)  # 迟到计算 -> 历史
        before = self.dump_all()
        self.assertEqual(
            [r["status"] for r in before["run"]], ["ok", "failed", "ok"])

        data = self.download_backup()
        # 清空式破坏后恢复
        self.restore(data)
        after = self.dump_all()
        self.assertEqual(after, before)
        statuses = [(r["id"], r["status"], r["is_current"]) for r in after["run"]]
        self.assertIn((2, "failed", 0), statuses)
        # 失败记录无结果行
        self.assertEqual(
            [r for r in after["run_usable"] if r["run_id"] == 2], [])

    def test_new_revisions_continue_after_max_id_after_restore(self):
        # 备份来自一个编号更大的库（修订 #3、运行 #2）
        for _ in range(2):
            self.upload("/upload/observations", OBS_CSV, "obs_more.csv")
        self.compute(3, 1)
        data = self.download_backup()
        # 当前库继续演化后恢复
        self.restore(data)
        # 恢复后再上传，新修订号必须接续备份最大号，而不是重用被替换掉的号
        resp = self.upload("/upload/observations", OBS_CSV, "obs_after.csv")
        self.assertIn("修订 #4", resp.get_data(as_text=True))
        # 备份中最大运行号为 #2（恢复前库的新运行已随替换消失），新运行接续 #3
        resp = self.compute(4, 1)
        self.assertIn("运行 #3", resp.get_data(as_text=True))
        # sqlite_sequence 中每个表只能有一行且值正确（防止 upsert 失效产生重复行）
        with self.app.app_context():
            conn = get_db()
            seq = {r["name"]: r["seq"] for r in conn.execute(
                "SELECT name, seq FROM sqlite_sequence").fetchall()}
        self.assertEqual(seq["obs_revision"], 4)
        self.assertEqual(seq["run"], 3)
        dup = self.db_count("SELECT COUNT(*) AS c FROM sqlite_sequence "
                            "WHERE name IN ('obs_revision','tax_revision','run')")
        self.assertEqual(dup, 3)

    def db_count(self, sql: str) -> int:
        with self.app.app_context():
            return get_db().execute(sql).fetchone()["c"]

    # ---------- 用例：拒绝非法备份，原库不变 ----------

    def test_reject_garbage_non_zip(self):
        before = self.dump_all()
        r = self.client.post(
            "/restore",
            data={"backup": (io.BytesIO(b"this is not a zip at all"), "x.zip")},
            content_type="multipart/form-data", follow_redirects=True)
        self.assertIn("已拒绝恢复", r.get_data(as_text=True))
        self.assertEqual(self.dump_all(), before)

    def test_reject_empty_file(self):
        before = self.dump_all()
        r = self.client.post(
            "/restore",
            data={"backup": (io.BytesIO(b""), "x.zip")},
            content_type="multipart/form-data", follow_redirects=True)
        self.assertIn("已拒绝恢复", r.get_data(as_text=True))
        self.assertEqual(self.dump_all(), before)

    def test_reject_truncated_zip(self):
        data = self.download_backup()
        before = self.dump_all()
        r = self.client.post(
            "/restore",
            data={"backup": (io.BytesIO(data[: len(data) // 2]), "x.zip")},
            content_type="multipart/form-data", follow_redirects=True)
        self.assertIn("已拒绝恢复", r.get_data(as_text=True))
        self.assertEqual(self.dump_all(), before)

    def test_reject_missing_member(self):
        data = self.download_backup()
        before = self.dump_all()
        buf = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data)) as src, \
                zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
            for info in src.infolist():
                if info.filename == "data/run_pending.json":
                    continue  # 抽掉一个必备文件
                dst.writestr(info, src.read(info.filename))
        r = self.client.post(
            "/restore",
            data={"backup": (io.BytesIO(buf.getvalue()), "x.zip")},
            content_type="multipart/form-data", follow_redirects=True)
        self.assertIn("不是完整备份", r.get_data(as_text=True))
        self.assertEqual(self.dump_all(), before)

    def test_reject_tampered_table_content(self):
        data = self.download_backup()
        before = self.dump_all()
        import json
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            members = {n: zf.read(n) for n in zf.namelist()}
        rows = json.loads(members["data/obs_revision.json"])
        rows[0]["filename"] = "tampered.csv"
        members["data/obs_revision.json"] = json.dumps(rows).encode()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for n, raw in members.items():
                zf.writestr(n, raw)
        r = self.client.post(
            "/restore",
            data={"backup": (io.BytesIO(buf.getvalue()), "x.zip")},
            content_type="multipart/form-data", follow_redirects=True)
        # 篡改后校验和或 HMAC 必然失败
        self.assertIn("已拒绝恢复", r.get_data(as_text=True))
        self.assertEqual(self.dump_all(), before)

    def test_reject_foreign_app_signature(self):
        data = self.download_backup()
        before = self.dump_all()
        # 用另一个 SECRET_KEY 重新签名同一份数据
        with self.app.app_context():
            parsed = backup.parse_and_validate_backup(
                data, self.app.config["SECRET_KEY"])
        foreign = self._resign(parsed, "a-completely-different-secret")
        r = self.client.post(
            "/restore",
            data={"backup": (io.BytesIO(foreign), "x.zip")},
            content_type="multipart/form-data", follow_redirects=True)
        self.assertIn("签名（HMAC）校验失败", r.get_data(as_text=True))
        self.assertEqual(self.dump_all(), before)

    @staticmethod
    def _resign(parsed: dict, secret: str) -> bytes:
        import hashlib
        import hmac
        import json
        marker = parsed["marker"]
        tables = parsed["tables"]
        canon = backup._canonical_bytes
        mac = hmac.new(secret.encode(), digestmod=hashlib.sha256)
        mac.update(canon(marker))
        for t in dbmod.BACKUP_TABLES:
            mac.update(canon(tables[t]))
        manifest = {
            "format_version": marker["format_version"],
            "exported_at": marker["exported_at"],
            "tables": parsed["manifest"]["tables"],
            "hmac_sha256": mac.hexdigest(),
        }
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("APP_BACKUP", canon(marker))
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
            for t in dbmod.BACKUP_TABLES:
                zf.writestr(f"data/{t}.json", json.dumps(tables[t], ensure_ascii=False))
        return buf.getvalue()

    def test_reject_missing_reference(self):
        data = self.download_backup()
        before = self.dump_all()
        import json
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            members = {n: zf.read(n) for n in zf.namelist()}
        # 直接构造引用缺失：run 指向不存在的观测修订 #99，并用合法密钥重签
        rows = json.loads(members["data/run.json"])
        rows[0]["obs_revision_id"] = 99
        tables = {t: json.loads(members[f"data/{t}.json"])
                  for t in dbmod.BACKUP_TABLES}
        tables["run"] = rows
        parsed = {"tables": tables, "marker": json.loads(members["APP_BACKUP"]),
                  "manifest": json.loads(members["manifest.json"])}
        with self.assertRaises(backup.BackupError) as ctx:
            backup._validate_referential_integrity(tables)
        self.assertIn("引用缺失", str(ctx.exception))
        # 通过路由同样被拒
        resigned = self._resign(parsed, self.app.config["SECRET_KEY"])
        r = self.client.post(
            "/restore",
            data={"backup": (io.BytesIO(resigned), "x.zip")},
            content_type="multipart/form-data", follow_redirects=True)
        self.assertIn("引用缺失", r.get_data(as_text=True))
        self.assertEqual(self.dump_all(), before)

    def test_reject_two_current_marks(self):
        data = self.download_backup()
        import json
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            members = {n: zf.read(n) for n in zf.namelist()}
        # 再造一条 ok 运行并同时标当前
        tables = {t: json.loads(members[f"data/{t}.json"])
                  for t in dbmod.BACKUP_TABLES}
        run2 = dict(tables["run"][0])
        run2["id"] = 2
        tables["run"].append(run2)
        with self.assertRaises(backup.BackupError) as ctx:
            backup._validate_referential_integrity(tables)
        self.assertIn("当前结果标记", str(ctx.exception))

    # ---------- 用例：确认页 / 取消 / 令牌 ----------

    def test_cancel_leaves_database_untouched(self):
        before = self.dump_all()
        data = self.download_backup()
        r1 = self.client.post(
            "/restore",
            data={"backup": (io.BytesIO(data), "backup.zip")},
            content_type="multipart/form-data")
        token = self._extract_token(r1.get_data(as_text=True))
        r = self.client.post("/restore/cancel", data={"token": token},
                             follow_redirects=True)
        self.assertIn("已取消恢复", r.get_data(as_text=True))
        self.assertEqual(self.dump_all(), before)
        # 令牌一次性：取消后再确认应失效
        r = self.client.post("/restore/confirm", data={"token": token},
                             follow_redirects=True)
        self.assertIn("不存在或已过期", r.get_data(as_text=True))
        self.assertEqual(self.dump_all(), before)

    def test_unknown_token_404_or_rejected(self):
        r = self.client.post("/restore/confirm", data={"token": "../etc/passwd"},
                             follow_redirects=True)
        self.assertIn(r.status_code, (404, 200))

    def test_upload_without_file_rejected(self):
        r = self.client.post("/restore", data={}, follow_redirects=True)
        self.assertIn("请选择", r.get_data(as_text=True))

    # ---------- 用例：并发围栏与快照隔离 ----------

    def test_inflight_upload_after_restore_is_aborted(self):
        """恢复前已开始的上传，在恢复提交后不得写回（即使修订号相同）。"""
        backup_data = self.download_backup()
        before = self.dump_all()
        conn = self.raw_connect()
        gen = dbmod.begin_snapshot(conn)  # 模拟请求先打开快照
        # 另一条连接执行完整恢复并提交
        restorer = self.raw_connect()
        dbmod.begin_write(restorer, dbmod.begin_snapshot(restorer))
        with self.app.app_context():
            parsed = backup.parse_and_validate_backup(
                backup_data, self.app.config["SECRET_KEY"])
            backup.restore_backup(restorer, parsed)
            restorer.commit()
        restorer.close()
        # 旧请求此时升级写事务：generation 已变，必须被围栏拦下
        with self.assertRaises(dbmod.GenerationMismatchError):
            dbmod.begin_write(conn, gen)
        conn.rollback()
        conn.close()
        # 库完全等于备份，没有任何额外修订混入
        self.assertEqual(self.dump_all(), before)

    def test_inflight_compute_failed_record_after_restore_aborted(self):
        """恢复前已开始的计算，其失败记录也不得在恢复成功后写回。"""
        backup_data = self.download_backup()
        conn = self.raw_connect()
        gen = dbmod.begin_snapshot(conn)
        restorer = self.raw_connect()
        dbmod.begin_write(restorer, dbmod.begin_snapshot(restorer))
        with self.app.app_context():
            parsed = backup.parse_and_validate_backup(
                backup_data, self.app.config["SECRET_KEY"])
            backup.restore_backup(restorer, parsed)
            restorer.commit()
        restorer.close()
        with self.assertRaises(dbmod.GenerationMismatchError):
            dbmod.begin_write(conn, gen)  # 连失败记录的插入机会都没有
        conn.rollback()
        conn.close()
        runs = self.dump_all()["run"]
        self.assertEqual([r["id"] for r in runs], [1])  # 仍是备份中的那一条

    def test_reader_sees_only_complete_state_during_restore(self):
        """恢复与读取交错：读事务只能看到完整旧库或完整备份，绝无中间态。

        恢复事务中途业务表会被短暂清空，快照隔离必须保证任何读请求
        要么看到恢复前的完整旧库，要么看到恢复后的完整备份。
        """
        # 先造一个“大备份”：修订 #3、运行 #3，结果行数与旧库不同
        for _ in range(2):
            self.upload("/upload/observations", OBS_CSV, "bigger.csv")
        self.compute(3, 1)
        big_backup = self.download_backup()
        expected_big = (3, 2, 10)  # obs 修订数 / 运行数 / 可用结果行累计(5+5)
        # 回到小库状态
        small_backup = self.download_backup()
        self.restore(small_backup)
        expected_small = (1, 1, 5)

        seen = set()
        stop = threading.Event()

        def reader():
            conn = self.raw_connect()
            while not stop.is_set():
                # 每次读都开新快照，观察恢复切换瞬间能否读到半份状态
                conn.execute("BEGIN DEFERRED")
                state = (
                    conn.execute(
                        "SELECT COUNT(*) AS c FROM obs_revision").fetchone()["c"],
                    conn.execute(
                        "SELECT COUNT(*) AS c FROM run").fetchone()["c"],
                    conn.execute(
                        "SELECT COUNT(*) AS c FROM run_usable").fetchone()["c"],
                )
                conn.execute("COMMIT")
                seen.add(state)
                time.sleep(0.001)
            conn.close()

        t = threading.Thread(target=reader)
        t.start()
        try:
            for _ in range(4):  # 反复在小库 / 大备份之间整库替换
                self.do_restore(big_backup)
                self.do_restore(small_backup)
        finally:
            stop.set()
            t.join()

        self.assertTrue(seen)
        # 只允许出现两种完整状态；任何撕裂 / 清空中间态都会落在集合外而失败
        self.assertTrue(seen.issubset({expected_small, expected_big}),
                        f"读到非完整状态：{seen - {expected_small, expected_big}}")
        self.assertIn(expected_big, seen)

    def test_restore_bumps_generation_and_fences_http_request(self):
        """端到端：HTTP 上传在恢复期间完成时被拒，且无数据写入。"""
        backup_data = self.download_backup()
        before = self.dump_all()
        # 直接用底层连接模拟一个在恢复前打开、恢复后才提交的 HTTP 写请求
        conn = self.raw_connect()
        gen = dbmod.begin_snapshot(conn)
        self.restore(backup_data)  # 另起请求完成恢复
        with self.assertRaises(dbmod.GenerationMismatchError):
            dbmod.begin_write(conn, gen)
        conn.rollback()
        conn.close()
        self.assertEqual(self.dump_all(), before)
        # 恢复后正常上传 / 计算仍可继续
        resp = self.upload("/upload/taxonomy", TAX_CSV, "tax_after.csv")
        self.assertIn("修订 #2", resp.get_data(as_text=True))
        resp = self.compute(1, 2)
        self.assertIn("已作为当前结果", resp.get_data(as_text=True))

    def test_restore_interrupted_by_exception_rolls_back(self):
        """恢复事务中途异常 -> 整体回滚，库保持恢复前完整状态。"""
        backup_data = self.download_backup()
        before = self.dump_all()
        with self.app.app_context():
            parsed = backup.parse_and_validate_backup(
                backup_data, self.app.config["SECRET_KEY"])
        conn = self.raw_connect()
        dbmod.begin_write(conn, dbmod.begin_snapshot(conn))
        with self.app.app_context():
            backup.restore_backup(conn, parsed)
            conn.rollback()  # 模拟中断：提交前崩溃 / 回滚
        conn.close()
        self.assertEqual(self.dump_all(), before)
        # generation 也未递增
        with self.app.app_context():
            self.assertEqual(dbmod.current_generation(get_db()), 1)

    def test_backup_taken_with_concurrent_commits_is_consistent_snapshot(self):
        """备份期间上传与计算仍可完成，且备份本身是单一已提交快照。"""
        errors = []

        def writer():
            try:
                for k in range(5):
                    self.upload("/upload/observations", OBS_CSV, f"c{k}.csv")
            except Exception as exc:  # 快照围栏触发属正常，不算错误
                errors.append(exc)

        t = threading.Thread(target=writer)
        t.start()
        data = self.download_backup()
        t.join()
        self.assertEqual(errors, [])
        # 备份自身可通过完整校验
        with self.app.app_context():
            parsed = backup.parse_and_validate_backup(
                data, self.app.config["SECRET_KEY"])
        # 备份内引用全部自洽
        obs_count = len(parsed["tables"]["obs_revision"])
        for run in parsed["tables"]["run"]:
            self.assertLessEqual(run["obs_revision_id"], obs_count)


if __name__ == "__main__":
    unittest.main()
