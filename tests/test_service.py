import unittest

from service_09261_011.workflow import Workflow
from service_09261_011.store import SQLiteStore
from service_09261_011.api import dispatch


def _item(i, actor="alice", content="ok"):
    return {"id": i, "actor": actor, "content": content}


class TestFlow(unittest.TestCase):
    def test_flow(self):
        f = Workflow()
        self.assertEqual(f.create("c1", "a", "k").state, "draft")
        self.assertEqual(f.create("c1", "a", "k").version, 1)
        self.assertEqual(f.move("c1", "reviewing", "b").state, "reviewing")

    def test_batch_partial_failure_does_not_block_valid_items(self):
        f = Workflow()
        result = f.import_batch("b1", [
            _item("c1"),
            {"id": "c2"},  # 缺 actor/content
            "not-an-object",
            _item("c3", "bob"),
            _item("c3", "bob"),  # 批内重复
        ])
        self.assertFalse(result["replayed"])
        self.assertEqual(result["imported"], ["c1", "c3"])
        reasons = {(x["index"], x["reason"].split()[0]) for x in result["failed"]}
        self.assertIn((1, "missing"), reasons)
        self.assertIn((2, "item"), reasons)
        self.assertIn((4, "duplicate"), reasons)
        # 合法条目直接进入待审查，且带着批次和负责人关系
        self.assertEqual(f.rows["c1"].state, "pending")
        self.assertEqual(f.rows["c1"].batch, "b1")
        self.assertEqual(f.rows["c1"].owner, "alice")

    def test_same_batch_redelivered_is_replayed_without_duplicates(self):
        f = Workflow()
        first = f.import_batch("b1", [_item("c1"), {"id": "bad"}])
        second = f.import_batch("b1", [_item("c1"), _item("c2")])
        self.assertTrue(second["replayed"])
        self.assertEqual({k: v for k, v in second.items() if k != "replayed"},
                         {k: v for k, v in first.items() if k != "replayed"})
        self.assertEqual(len(f.rows), 1)  # 重放不新建任何记录

    def test_owner_confirms_and_requests_fix_others_forbidden(self):
        f = Workflow()
        f.import_batch("b1", [_item("c1", "alice"), _item("c2", "bob")])
        with self.assertRaises(ValueError):
            f.confirm("c1", "bob")
        f.confirm("c1", "alice")
        f.request_fix("c2", "bob", "引用来源缺失")
        self.assertEqual(f.rows["c1"].state, "confirmed")
        self.assertEqual(f.rows["c2"].state, "needs_fix")
        self.assertEqual(f.rows["c2"].note, "引用来源缺失")
        # 修正后重新提交回到待处理
        self.assertEqual(f.resubmit("c2", "bob").state, "pending")

    def test_progress_groups_by_three_states(self):
        f = Workflow()
        f.import_batch("b1", [_item("c1"), _item("c2"), _item("c3")])
        f.confirm("c1", "alice")
        f.request_fix("c2", "alice", "改")
        p = f.progress("b1")
        self.assertEqual(p["counts"],
                         {"pending": 1, "confirmed": 1, "needs_fix": 1})
        self.assertEqual([x["id"] for x in p["states"]["confirmed"]], ["c1"])

    def test_persistence_survives_reload_and_keeps_batch_idempotency(self):
        store = SQLiteStore(":memory:")
        f = Workflow(store)
        f.import_batch("b1", [_item("c1"), {"id": "x"}])
        f.confirm("c1", "alice")
        g = Workflow(store)
        self.assertEqual(g.rows["c1"].state, "confirmed")
        replay = g.import_batch("b1", [_item("c1"), _item("c2")])
        self.assertTrue(replay["replayed"])
        self.assertNotIn("c2", g.rows)
        self.assertEqual(g.progress("b1")["counts"]["confirmed"], 1)

    def test_api_batch_and_progress(self):
        f = Workflow()
        code, body = dispatch(f, "POST", "/batches",
                              {"batch": "b1", "items": [_item("c1"), {}]})
        self.assertEqual(code, 200)
        self.assertEqual(body["imported"], ["c1"])
        self.assertEqual(len(body["failed"]), 1)
        code, body = dispatch(f, "GET", "/progress", query={"batch": "b1"})
        self.assertEqual(code, 200)
        self.assertEqual(body["counts"]["pending"], 1)
        code, body = dispatch(f, "POST", "/cases/c1/confirm", {"actor": "alice"})
        self.assertEqual(code, 200)
        self.assertEqual(body["state"], "confirmed")
        # 非负责人操作得到明确 400
        code, body = dispatch(f, "POST", "/cases/c1/confirm", {"actor": "eve"})
        self.assertEqual(code, 400)
        self.assertIn("forbidden", body["detail"])


if __name__ == "__main__":
    unittest.main()
