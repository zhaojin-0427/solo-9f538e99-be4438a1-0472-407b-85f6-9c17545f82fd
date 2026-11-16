"""关键功能自测：纯逻辑、CSV 校验、网页流程、修订/历史/失败语义。

运行：python3 -m unittest discover -s tests -v
"""
import io
import os
import tempfile
import unittest
from unittest import mock

from app import create_app
from app import taxonomy
from app.db import get_db

OBS_CSV = (
    "record_no,initial_taxon,quantity\n"
    "R1,A,10\n"
    "R2,X,5\n"
    "R3,Z,3\n"
    "R4,B,2\n"
    "R5,Q,7\n"
    "R6,M,4\n"
)
TAX_CSV = (
    "round,old_id,new_id\n"
    "1,A,B\n"
    "1,A,C\n"
    "1,M,N\n"
    "1,M,O\n"
    "1,X,Y\n"
    "1,Z,Y\n"
    "2,B,D\n"
    "2,C,D\n"
    "2,N,D\n"
    "2,Y,Y2\n"
    "3,D,E\n"
)


class TraceLogicTest(unittest.TestCase):
    """逐轮追踪的纯逻辑：拆分、归并、沿用、多轮链条。"""

    def setUp(self):
        self.rounds_map, self.max_round, _ = taxonomy.parse_taxonomy(TAX_CSV)

    def test_split_then_merge_is_usable(self):
        finals, _ = taxonomy.trace_one("A", self.rounds_map, self.max_round)
        self.assertEqual(finals, {"E"})

    def test_split_without_merge_is_pending(self):
        finals, split_round = taxonomy.trace_one("M", self.rounds_map, self.max_round)
        self.assertEqual(finals, {"E", "O"})
        self.assertEqual(split_round, 1)

    def test_many_to_one_merge(self):
        self.assertEqual(taxonomy.trace_one("X", self.rounds_map, self.max_round)[0],
                         {"Y2"})
        self.assertEqual(taxonomy.trace_one("Z", self.rounds_map, self.max_round)[0],
                         {"Y2"})

    def test_carry_over_then_mapped_later(self):
        # B 在第 1 轮未列出（沿用），第 2 轮映射为 D，第 3 轮为 E
        self.assertEqual(taxonomy.trace_one("B", self.rounds_map, self.max_round)[0],
                         {"E"})

    def test_unlisted_taxon_carries_through(self):
        self.assertEqual(taxonomy.trace_one("Q", self.rounds_map, self.max_round)[0],
                         {"Q"})

    def test_compute_results_counts(self):
        obs = taxonomy.parse_observations(OBS_CSV)
        usable, pending = taxonomy.compute_results(obs, self.rounds_map, self.max_round)
        self.assertEqual(len(usable), 5)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["record_no"], "R6")
        self.assertEqual(pending[0]["quantity"], 4)  # 数量保留不分摊
        self.assertIn("E、O", pending[0]["reason"])
        by_rec = {r["record_no"]: r["final_taxon"] for r in usable}
        self.assertEqual(by_rec, {"R1": "E", "R2": "Y2", "R3": "Y2",
                                  "R4": "E", "R5": "Q"})


class ParseValidationTest(unittest.TestCase):
    """CSV 校验：行数上限、唯一记录号、非负整数、轮次连续。"""

    def test_obs_duplicate_record_no(self):
        with self.assertRaises(taxonomy.CsvError) as ctx:
            taxonomy.parse_observations("record_no,initial_taxon,quantity\nR1,A,1\nR1,B,2\n")
        self.assertIn("重复", str(ctx.exception))

    def test_obs_negative_quantity(self):
        with self.assertRaises(taxonomy.CsvError) as ctx:
            taxonomy.parse_observations("record_no,initial_taxon,quantity\nR1,A,-1\n")
        self.assertIn("非负整数", str(ctx.exception))

    def test_obs_non_integer_quantity(self):
        with self.assertRaises(taxonomy.CsvError):
            taxonomy.parse_observations("record_no,initial_taxon,quantity\nR1,A,1.5\n")

    def test_obs_too_many_rows(self):
        rows = "".join(f"R{i},A,1\n" for i in range(101))
        with self.assertRaises(taxonomy.CsvError) as ctx:
            taxonomy.parse_observations("record_no,initial_taxon,quantity\n" + rows)
        self.assertIn("100", str(ctx.exception))

    def test_obs_exactly_100_rows_ok(self):
        rows = "".join(f"R{i},A,1\n" for i in range(100))
        self.assertEqual(len(taxonomy.parse_observations(
            "record_no,initial_taxon,quantity\n" + rows)), 100)

    def test_obs_missing_column(self):
        with self.assertRaises(taxonomy.CsvError) as ctx:
            taxonomy.parse_observations("record_no,quantity\nR1,1\n")
        self.assertIn("initial_taxon", str(ctx.exception))

    def test_tax_too_many_rows(self):
        rows = "".join(f"1,A{i},B{i}\n" for i in range(41))
        with self.assertRaises(taxonomy.CsvError) as ctx:
            taxonomy.parse_taxonomy("round,old_id,new_id\n" + rows)
        self.assertIn("40", str(ctx.exception))

    def test_tax_rounds_must_be_consecutive(self):
        with self.assertRaises(taxonomy.CsvError) as ctx:
            taxonomy.parse_taxonomy("round,old_id,new_id\n1,A,B\n3,B,C\n")
        self.assertIn("连续", str(ctx.exception))

    def test_tax_round_out_of_range(self):
        with self.assertRaises(taxonomy.CsvError):
            taxonomy.parse_taxonomy("round,old_id,new_id\n5,A,B\n")

    def test_tax_empty_rejected(self):
        with self.assertRaises(taxonomy.CsvError):
            taxonomy.parse_taxonomy("round,old_id,new_id\n")

    def test_chinese_headers_and_gb18030(self):
        text = "记录号,初始分类号,数量\nR1,甲,3\n"
        data = text.encode("gb18030")
        decoded = taxonomy.decode_csv_bytes(data)
        rows = taxonomy.parse_observations(decoded)
        self.assertEqual(rows[0]["initial_taxon"], "甲")


class WebFlowTest(unittest.TestCase):
    """网页流程：上传、计算、更正、历史、失败。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app({
            "DB_PATH": os.path.join(self.tmp.name, "test.db"),
            "SECRET_KEY": "test",
        })
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    # ---------- 辅助 ----------

    def upload(self, url, text, filename="data.csv"):
        return self.client.post(
            url,
            data={"file": (io.BytesIO(text.encode("utf-8")), filename)},
            content_type="multipart/form-data",
            follow_redirects=True)

    def upload_both(self, obs=OBS_CSV, tax=TAX_CSV):
        self.upload("/upload/observations", obs, "obs.csv")
        self.upload("/upload/taxonomy", tax, "tax.csv")

    def compute(self, obs_id, tax_id):
        return self.client.post(
            "/compute",
            data={"obs_revision_id": obs_id, "tax_revision_id": tax_id},
            follow_redirects=True)

    def db(self, sql, args=()):
        with self.app.app_context():
            return get_db().execute(sql, args).fetchall()

    # ---------- 用例 ----------

    def test_upload_compute_and_index_shows_results(self):
        self.upload_both()
        resp = self.compute(1, 1)
        html = resp.get_data(as_text=True)
        self.assertIn("已作为当前结果", html)
        self.assertIn("可用 5 条", html)
        self.assertIn("待核 1 条", html)
        self.assertIn("R6", html)
        self.assertIn("数量不能分摊", html)
        runs = self.db("SELECT * FROM run")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["is_current"], 1)
        self.assertEqual(runs[0]["obs_revision_id"], 1)
        self.assertEqual(runs[0]["tax_revision_id"], 1)
        self.assertEqual(len(self.db("SELECT * FROM run_usable")), 5)
        self.assertEqual(len(self.db("SELECT * FROM run_pending")), 1)
        # 首页展示当前结果与待核原因
        index = self.client.get("/").get_data(as_text=True)
        self.assertIn("当前结果", index)
        self.assertIn("待核", index)

    def test_invalid_upload_saved_nothing(self):
        resp = self.upload("/upload/observations",
                           "record_no,initial_taxon,quantity\nR1,A,-3\n")
        self.assertIn("非负整数", resp.get_data(as_text=True))
        self.assertEqual(self.db("SELECT * FROM obs_revision"), [])
        self.assertEqual(self.db("SELECT * FROM run"), [])

    def test_correction_invalidates_current_but_keeps_history(self):
        self.upload_both()
        self.compute(1, 1)
        # 整份更正观测 CSV
        new_obs = OBS_CSV + "R7,Q,1\n"
        resp = self.upload("/upload/observations", new_obs, "obs2.csv")
        self.assertIn("修订 #2", resp.get_data(as_text=True))
        # 旧当前结果已失效
        self.assertEqual(self.db("SELECT * FROM run WHERE is_current = 1"), [])
        index = self.client.get("/").get_data(as_text=True)
        self.assertIn("暂无当前结果", index)
        # 旧结果仍可查
        detail = self.client.get("/runs/1")
        self.assertEqual(detail.status_code, 200)
        self.assertIn("R6", detail.get_data(as_text=True))
        # 用最新修订重新计算 -> 新的当前结果
        self.compute(2, 1)
        currents = self.db("SELECT * FROM run WHERE is_current = 1")
        self.assertEqual(len(currents), 1)
        self.assertEqual(currents[0]["obs_revision_id"], 2)
        self.assertEqual(currents[0]["usable_count"], 6)

    def test_late_computation_only_kept_as_history(self):
        self.upload_both()
        self.compute(1, 1)
        self.upload("/upload/observations", OBS_CSV + "R7,Q,1\n", "obs2.csv")
        # 迟到计算：固定旧的观测修订 #1
        resp = self.compute(1, 1)
        self.assertIn("仅留作历史", resp.get_data(as_text=True))
        late = self.db("SELECT * FROM run ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual(late["is_current"], 0)
        self.assertIn("历史", late["note"])
        # 不会挤占/产生当前结果
        self.assertEqual(self.db("SELECT * FROM run WHERE is_current = 1"), [])

    def test_failed_run_has_no_partial_results(self):
        self.upload_both()
        with mock.patch("app.taxonomy.compute_results",
                        side_effect=RuntimeError("boom")):
            resp = self.compute(1, 1)
        self.assertIn("未开放任何部分结果", resp.get_data(as_text=True))
        runs = self.db("SELECT * FROM run")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "failed")
        self.assertEqual(runs[0]["is_current"], 0)
        self.assertEqual(self.db("SELECT * FROM run_usable"), [])
        self.assertEqual(self.db("SELECT * FROM run_pending"), [])
        # 失败记录详情页可打开且不含结果表
        detail = self.client.get(f"/runs/{runs[0]['id']}").get_data(as_text=True)
        self.assertIn("失败原因", detail)

    def test_compute_without_inputs(self):
        resp = self.client.post("/compute", data={}, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.db("SELECT * FROM run"), [])

    def test_revision_download_roundtrip(self):
        self.upload_both()
        resp = self.client.get("/revisions/obs/1/download")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_data(as_text=True), OBS_CSV)
        self.assertEqual(self.client.get("/revisions/obs/99/download").status_code, 404)

    def test_pasted_text_upload(self):
        resp = self.client.post(
            "/upload/taxonomy", data={"pasted": TAX_CSV}, follow_redirects=True)
        self.assertIn("修订 #1", resp.get_data(as_text=True))

    def test_gb18030_file_upload(self):
        text = "记录号,初始分类号,数量\nR1,甲,3\n"
        resp = self.client.post(
            "/upload/observations",
            data={"file": (io.BytesIO(text.encode("gb18030")), "obs.csv")},
            content_type="multipart/form-data",
            follow_redirects=True)
        self.assertIn("修订 #1", resp.get_data(as_text=True))

    def test_run_detail_404(self):
        self.assertEqual(self.client.get("/runs/42").status_code, 404)


if __name__ == "__main__":
    unittest.main()
