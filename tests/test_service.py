import os
import tempfile
import unittest

from service_09261_011.workflow import Workflow
from service_09261_011.store import SQLiteStore
from service_09261_011.api import dispatch


def make_items():
    return [
        {"id": "c1", "content": "文章一", "owner": "alice"},
        {"content": "没有 id 的文章", "owner": "bob"},          # 合法，自动生成 id
        {"id": "c3", "content": "", "owner": "alice"},          # 坏：内容为空
        {"id": "c4", "owner": "bob"},                            # 坏：缺 content
        "not-an-object",                                          # 坏：结构错误
        {"id": "c6", "content": "文章六", "owner": "alice"},
        {"id": "c1", "content": "重复 id", "owner": "carol"},    # 坏：批内 id 重复
    ]


class TestBatchImport(unittest.TestCase):
    def setUp(self):
        self.flow = Workflow()

    def test_bad_rows_do_not_block_batch(self):
        report = self.flow.import_batch("b1", make_items(), actor="importer")
        self.assertEqual(report["total"], 7)
        self.assertEqual(report["accepted_count"], 3)
        self.assertEqual(report["failed_count"], 4)
        # 合法条目全部进入待处理
        for case_id in report["accepted"]:
            self.assertEqual(self.flow.rows[case_id].state, "pending")
        # 失败条目带明确原因和原始下标
        first = report["failures"][0]
        self.assertEqual(first["index"], 2)
        self.assertTrue(any("content" in r for r in first["reasons"]))
        self.assertTrue(any("JSON 对象" in r for r in report["failures"][2]["reasons"]))

    def test_batch_and_owner_relation_kept(self):
        report = self.flow.import_batch("b1", make_items())
        for case_id in report["accepted"]:
            self.assertEqual(self.flow.rows[case_id].batch_id, "b1")
        alice = self.flow.snapshot(batch_id="b1", owner="alice")
        self.assertEqual({c["id"] for c in alice}, {"c1", "c6"})
        bobs = self.flow.snapshot(owner="bob")
        self.assertEqual(len(bobs), 1)
        self.assertTrue(bobs[0]["id"].startswith("gen-"))

    def test_same_batch_redelivered_is_idempotent(self):
        first = self.flow.import_batch("b1", make_items())
        rows_after_first = dict(self.flow.rows)
        second = self.flow.import_batch("b1", make_items())
        self.assertTrue(second["replayed"])
        self.assertEqual(second["accepted"], first["accepted"])
        # 没有任何新记录被创建
        self.assertEqual(set(self.flow.rows), set(rows_after_first))

    def test_generated_ids_are_deterministic(self):
        f2 = Workflow()
        r1 = self.flow.import_batch("bX", [{"content": "同内容", "owner": "o"}])
        r2 = f2.import_batch("bX", [{"content": "同内容", "owner": "o"}])
        self.assertEqual(r1["accepted"], r2["accepted"])

    def test_progress_buckets(self):
        report = self.flow.import_batch("b1", make_items())
        self.flow.move("c1", "approved", "lead")
        self.flow.move("c6", "rejected", "lead", reason="含敏感词，需修改")

        progress = self.flow.progress(batch_id="b1")
        self.assertEqual(progress["counts"],
                         {"pending": 1, "confirmed": 1, "needs_fix": 5})
        self.assertEqual({c["id"] for c in progress["confirmed"]}, {"c1"})
        self.assertEqual(len(progress["pending"]), 1)
        kinds = {item["kind"] for item in progress["needs_fix"]}
        self.assertEqual(kinds, {"rejected", "import_failure"})
        rejected = next(i for i in progress["needs_fix"] if i["kind"] == "rejected")
        self.assertEqual(rejected["reason"], "含敏感词，需修改")
        failures = [i for i in progress["needs_fix"] if i["kind"] == "import_failure"]
        self.assertEqual(len(failures), 4)

        # 按负责人过滤：alice 看到 c1 已确认、c6 需修正、c3 导入失败
        alice = self.flow.progress(owner="alice")
        self.assertEqual(alice["counts"],
                         {"pending": 0, "confirmed": 1, "needs_fix": 2})

    def test_rejected_can_be_fixed_and_resubmitted(self):
        self.flow.import_batch("b1", [{"id": "c1", "content": "x", "owner": "a"}])
        self.flow.move("c1", "rejected", "lead", reason="整改")
        self.flow.move("c1", "pending", "a")
        self.assertEqual(self.flow.rows["c1"].state, "pending")
        self.flow.move("c1", "approved", "lead")
        self.assertEqual(self.flow.rows["c1"].version, 4)

    def test_invalid_batch_payload(self):
        with self.assertRaises(ValueError):
            self.flow.import_batch("", [])
        with self.assertRaises(ValueError):
            self.flow.import_batch("b", "not-a-list")


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.path = self.tmp.name

    def tearDown(self):
        os.unlink(self.path)
        for suffix in ("-wal", "-shm"):
            p = self.path + suffix
            if os.path.exists(p):
                os.unlink(p)

    def test_redelivery_after_reopen_does_not_duplicate(self):
        store = SQLiteStore(self.path)
        flow = Workflow(store)
        first = flow.import_batch("b1", make_items())
        flow.move("c1", "approved", "lead")

        # 重新连接：批次关系、条目、确认结果全部恢复
        reopened = Workflow(SQLiteStore(self.path))
        self.assertEqual(reopened.rows["c1"].state, "approved")
        self.assertEqual(reopened.rows["c1"].batch_id, "b1")

        again = reopened.import_batch("b1", make_items())
        self.assertTrue(again["replayed"])
        self.assertEqual(again["accepted_count"], first["accepted_count"])
        self.assertEqual(len(reopened.snapshot()), first["accepted_count"])

        progress = reopened.progress(batch_id="b1")
        self.assertEqual(progress["counts"]["confirmed"], 1)
        self.assertEqual(progress["counts"]["needs_fix"], 4)

    def test_single_create_idempotency_key_persists(self):
        store = SQLiteStore(self.path)
        flow = Workflow(store)
        flow.create("c1", "a", "key-1")
        reopened = Workflow(SQLiteStore(self.path))
        self.assertEqual(reopened.create("c1", "a", "key-1").id, "c1")
        self.assertEqual(len(reopened.snapshot()), 1)


class TestApi(unittest.TestCase):
    def setUp(self):
        self.flow = Workflow()

    def test_batch_endpoints(self):
        status, report = dispatch(self.flow, "POST", "/batches",
                                  {"batch_id": "b1", "items": make_items()})
        self.assertEqual(status, 201)

        # 重复送达返回 200 回放，而非再次创建
        status, replay = dispatch(self.flow, "POST", "/batches",
                                  {"batch_id": "b1", "items": make_items()})
        self.assertEqual(status, 200)
        self.assertTrue(replay["replayed"])

        status, got = dispatch(self.flow, "GET", "/batches/b1")
        self.assertEqual(status, 200)
        self.assertEqual(got["failed_count"], 4)

        status, progress = dispatch(self.flow, "GET", "/batches/b1/progress?owner=alice")
        self.assertEqual(status, 200)
        self.assertIn("needs_fix", progress)

    def test_case_endpoints_and_errors(self):
        status, body = dispatch(self.flow, "POST", "/batches",
                                {"batch_id": "b1",
                                 "items": [{"id": "c1", "content": "x", "owner": "a"}]})
        self.assertEqual(status, 201)
        status, body = dispatch(self.flow, "POST", "/cases/c1/move",
                                {"state": "approved", "actor": "lead"})
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "approved")

        status, body = dispatch(self.flow, "GET", "/cases?batch_id=b1&state=approved")
        self.assertEqual(status, 200)
        self.assertEqual([c["id"] for c in body], ["c1"])

        status, body = dispatch(self.flow, "POST", "/cases/missing/move",
                                {"state": "approved", "actor": "lead"})
        self.assertEqual(status, 404)

        status, body = dispatch(self.flow, "POST", "/batches", {"batch_id": "b"})
        self.assertEqual(status, 400)

    def test_legacy_single_case_flow(self):
        status, body = dispatch(self.flow, "POST", "/cases",
                                {"id": "c1", "actor": "a", "idempotency_key": "k"})
        self.assertEqual(status, 201)
        self.assertEqual(body["state"], "draft")
        status, body = dispatch(self.flow, "POST", "/cases/c1/move",
                                {"state": "reviewing", "actor": "b"})
        self.assertEqual(status, 200)
        status, body = dispatch(self.flow, "GET", "/cases")
        self.assertEqual(status, 200)
        self.assertEqual(len(body), 1)


if __name__ == "__main__":
    unittest.main()
