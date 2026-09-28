"""版本化业务工作流：合规内容按批导入、负责人逐项确认、进度查询。"""
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import hashlib
import json

MAX_CONTENT_LEN = 5000

# 状态机：兼容原有 draft/reviewing 流程；批量导入的合法条目直接进入 pending。
TRANSITIONS = {
    "draft": {"reviewing", "cancelled"},
    "reviewing": {"approved", "rejected"},
    "pending": {"approved", "rejected"},
    "rejected": {"draft", "pending"},  # 修正后可重新送审
    "approved": {"archived"},
}

# 进度三桶：已确认 / 待处理 / 需要修正
PROGRESS_BUCKETS = {
    "approved": "confirmed",
    "pending": "pending",
    "rejected": "needs_fix",
}


def _now():
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Case:
    id: str
    actor: str
    state: str
    version: int = 1
    batch_id: str = ""
    owner: str = ""
    content: str = ""
    reason: str = ""
    decided_by: str = ""
    updated_at: str = ""

    def move(self, state, actor, reason=""):
        if state not in TRANSITIONS.get(self.state, set()):
            raise ValueError("invalid transition")
        # 仅驳回保留原因（需要修正的说明），其余流转清空。
        new_reason = reason if state == "rejected" else ""
        decided_by = actor if state in ("approved", "rejected") else self.decided_by
        return Case(self.id, actor, state, self.version + 1,
                    self.batch_id, self.owner, self.content,
                    new_reason, decided_by, _now())


def validate_item(raw, index, known_ids):
    """校验单条导入数据，返回 (干净条目, 原因列表)，二者恰有一个非空。"""
    if not isinstance(raw, dict):
        return None, ["第 %d 条：条目必须是 JSON 对象" % index]
    reasons = []
    content = raw.get("content")
    owner = raw.get("owner")
    item_id = raw.get("id")
    if not isinstance(content, str) or not content.strip():
        reasons.append("content 缺失或为空")
    elif len(content) > MAX_CONTENT_LEN:
        reasons.append("content 超过 %d 字上限" % MAX_CONTENT_LEN)
    if not isinstance(owner, str) or not owner.strip():
        reasons.append("owner 缺失或为空")
    if item_id is not None and (not isinstance(item_id, str) or not item_id.strip()):
        reasons.append("id 必须是非空字符串")
    elif isinstance(item_id, str) and item_id.strip() in known_ids:
        reasons.append("id 重复：%s" % item_id.strip())
    if reasons:
        return None, ["第 %d 条：%s" % (index, "；".join(reasons))]
    return {"id": item_id.strip() if item_id else None,
            "content": content.strip(), "owner": owner.strip()}, []


class Workflow:
    def __init__(self, store=None):
        self.store = store
        self.rows = {}
        self.keys = {}
        self.batches = {}  # batch_id -> 导入报告（含失败明细）
        if store is not None:
            self._load()

    # ---- 持久化 ----
    def _load(self):
        for d in self.store.list_items():
            fields = Case.__dataclass_fields__
            self.rows[d["id"]] = Case(**{k: d.get(k, "") for k in fields})
        for k, case_id in self.store.list_keys():
            self.keys[k] = case_id
        for batch_id, report in self.store.list_batches():
            self.batches[batch_id] = json.loads(report)

    def _persist_item(self, case):
        if self.store is not None:
            self.store.put_item(asdict(case))

    # ---- 单条创建（保留原接口）----
    def create(self, id, actor, key=None):
        if key is not None and key in self.keys:
            return self.rows[self.keys[key]]
        if id in self.rows:
            raise ValueError("duplicate")
        row = Case(id, actor, "draft", updated_at=_now())
        self.rows[id] = row
        self._persist_item(row)
        if key:
            self.keys[key] = id
            if self.store is not None:
                self.store.put_key(key, id)
        return row

    def move(self, id, state, actor, reason=""):
        if id not in self.rows:
            raise KeyError(id)
        row = self.rows[id].move(state, actor, reason)
        self.rows[id] = row
        self._persist_item(row)
        return row

    # ---- 批量导入 ----
    @staticmethod
    def _gen_id(batch_id, index, clean):
        digest = hashlib.sha1(
            "|".join([batch_id, str(index), clean["owner"], clean["content"]]).encode("utf-8")
        ).hexdigest()
        return "gen-%s" % digest[:16]

    def import_batch(self, batch_id, items, actor="system"):
        """按批导入：坏数据逐条记录原因，不影响合法条目；批次幂等。"""
        if not isinstance(batch_id, str) or not batch_id.strip():
            raise ValueError("batch_id 缺失或为空")
        if not isinstance(items, (list, tuple)):
            raise ValueError("items 必须是数组")
        batch_id = batch_id.strip()
        if batch_id in self.batches:
            # 同一批次再次送达：原样回放，不重复创建任何记录。
            report = dict(self.batches[batch_id])
            report["replayed"] = True
            return report

        known = set(self.rows)
        accepted, failures = [], []
        for index, raw in enumerate(items):
            clean, reasons = validate_item(raw, index, known)
            if reasons:
                failures.append({"index": index, "reasons": reasons, "raw": raw})
                continue
            item_id = clean["id"] or self._gen_id(batch_id, index, clean)
            known.add(item_id)
            case = Case(item_id, actor, "pending", 1, batch_id,
                        clean["owner"], clean["content"], updated_at=_now())
            self.rows[item_id] = case
            self._persist_item(case)
            accepted.append(item_id)

        report = {
            "batch_id": batch_id,
            "imported_by": actor,
            "imported_at": _now(),
            "total": len(items),
            "accepted_count": len(accepted),
            "failed_count": len(failures),
            "accepted": accepted,
            "failures": failures,
            "replayed": False,
        }
        self.batches[batch_id] = report
        if self.store is not None:
            self.store.put_batch(
                batch_id, json.dumps(report, ensure_ascii=False, default=str))
        return report

    def get_batch(self, batch_id):
        if batch_id not in self.batches:
            raise KeyError(batch_id)
        return self.batches[batch_id]

    # ---- 查询 ----
    def _select(self, batch_id=None, owner=None, state=None):
        rows = (self.rows[k] for k in sorted(self.rows))
        if batch_id is not None:
            rows = (r for r in rows if r.batch_id == batch_id)
        if owner is not None:
            rows = (r for r in rows if r.owner == owner)
        if state is not None:
            rows = (r for r in rows if r.state == state)
        return list(rows)

    def snapshot(self, batch_id=None, owner=None, state=None):
        return [asdict(r) for r in self._select(batch_id, owner, state)]

    def progress(self, batch_id=None, owner=None):
        """返回已确认 / 待处理 / 需要修正三个分组及其明细。"""
        groups = {"confirmed": [], "pending": [], "needs_fix": []}
        states = {}
        for case in self._select(batch_id, owner):
            d = asdict(case)
            states[case.state] = states.get(case.state, 0) + 1
            # approved 已确认；rejected 需修正；其余在途状态（pending/draft/…）算待处理。
            bucket = PROGRESS_BUCKETS.get(case.state, "pending")
            if bucket == "needs_fix":
                d["kind"] = "rejected"
            groups[bucket].append(d)

        # 导入阶段就被拦下的坏数据同样属于“需要修正”，并附明确原因。
        batch_ids = [batch_id] if batch_id else sorted(self.batches)
        for bid in batch_ids:
            for failure in self.batches[bid]["failures"]:
                raw_owner = failure.get("raw")
                raw_owner = raw_owner.get("owner") if isinstance(raw_owner, dict) else None
                if owner is not None and raw_owner != owner:
                    continue
                groups["needs_fix"].append({
                    "kind": "import_failure",
                    "batch_id": bid,
                    "index": failure["index"],
                    "reasons": failure["reasons"],
                    "raw": failure["raw"],
                })
                states["import_failed"] = states.get("import_failed", 0) + 1

        counts = {
            "pending": len(groups["pending"]),
            "confirmed": len(groups["confirmed"]),
            "needs_fix": len(groups["needs_fix"]),
        }
        return {"filters": {"batch_id": batch_id, "owner": owner},
                "counts": counts, "states": states, **groups}
