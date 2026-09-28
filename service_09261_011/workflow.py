"""版本化业务工作流：批量导入、逐项审查、幂等重放与进度查询。"""
from dataclasses import dataclass, asdict, replace

# 审查条目状态：pending 待处理 / confirmed 已确认 / needs_fix 需要修正
REVIEW_STATES = ("pending", "confirmed", "needs_fix")

_ALLOWED = {
    "draft": {"reviewing", "cancelled"},
    "reviewing": {"approved", "rejected"},
    "rejected": {"draft"},
    "approved": {"archived"},
    "pending": {"confirmed", "needs_fix"},
    "needs_fix": {"pending"},
}


@dataclass(frozen=True)
class Case:
    id: str
    actor: str
    state: str
    version: int = 1
    batch: str = None
    # owner 为条目归属负责人，流转操作人记录在 actor，归属关系始终保留
    owner: str = None
    note: str = None

    def move(self, state, actor):
        if state not in _ALLOWED.get(self.state, set()):
            raise ValueError("invalid transition")
        return replace(self, actor=actor, state=state, version=self.version + 1)


@dataclass
class Batch:
    key: str
    items: list
    failed: list

    def result(self, replayed):
        return {"batch": self.key, "replayed": replayed,
                "imported": list(self.items), "failed": list(self.failed)}


class Workflow:
    def __init__(self, store=None):
        self.rows = {}
        self.keys = {}
        self.batches = {}
        self.store = store
        if store is not None:
            self._load(store.latest())

    # ---- 单条命令（保留原有接口） ----
    def create(self, id, actor, key=None):
        if key in self.keys:
            return self.rows[self.keys[key]]
        if id in self.rows:
            raise ValueError("duplicate")
        row = Case(id, actor, "draft", owner=actor)
        self.rows[id] = row
        if key:
            self.keys[key] = id
        self._persist()
        return row

    def move(self, id, state, actor, note=None):
        row = self.rows[id].move(state, actor)
        if note is not None:
            row = replace(row, note=note)
        self.rows[id] = row
        self._persist()
        return row

    # ---- 批量导入 ----
    def import_batch(self, key, items):
        """同一 key 再次送达时原样返回首次结果，不创建任何重复记录。"""
        if not isinstance(key, str) or not key:
            raise ValueError("invalid batch key")
        if key in self.batches:
            return self.batches[key].result(replayed=True)
        if not isinstance(items, (list, tuple)):
            raise ValueError("items must be a list")

        batch = Batch(key, [], [])
        seen = set()
        valid = []
        for index, item in enumerate(items):
            reason = self._validate(item, seen)
            if reason:
                batch.failed.append({"index": index,
                                     "id": item.get("id") if isinstance(item, dict) else None,
                                     "reason": reason})
                continue
            seen.add(item["id"])
            valid.append(item)

        for item in valid:
            row = Case(item["id"], item["actor"], "pending",
                       batch=key, owner=item["actor"], note=None)
            self.rows[row.id] = row
            batch.items.append(row.id)

        self.batches[key] = batch
        self._persist()
        return batch.result(replayed=False)

    @staticmethod
    def _validate(item, seen):
        if not isinstance(item, dict):
            return "item must be an object"
        iid, actor = item.get("id"), item.get("actor")
        if not isinstance(iid, str) or not iid.strip():
            return "missing or invalid id"
        if not isinstance(actor, str) or not actor.strip():
            return "missing or invalid actor"
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            return "missing or invalid content"
        if iid in seen:
            return "duplicate id within batch"
        return None

    # ---- 逐项确认 / 退回修正（仅归属负责人可操作） ----
    def confirm(self, id, actor):
        return self._act(id, "confirmed", actor)

    def request_fix(self, id, actor, reason=None):
        return self._act(id, "needs_fix", actor, reason)

    def resubmit(self, id, actor, content=None):
        case = self._owned(id, actor)
        row = case.move("pending", actor)
        row = replace(row, note=None)
        self.rows[id] = row
        self._persist()
        return row

    def _act(self, id, state, actor, note=None):
        self._owned(id, actor)
        return self.move(id, state, actor, note)

    def _owned(self, id, actor):
        case = self.rows[id]
        if case.owner != actor:
            raise ValueError("forbidden: item is owned by %s" % case.owner)
        return case

    # ---- 进度查询 ----
    def progress(self, batch=None):
        if batch is not None and batch not in self.batches:
            raise KeyError(batch)
        ids = self.batches[batch].items if batch else sorted(self.rows)
        states = {s: [] for s in REVIEW_STATES}
        for iid in ids:
            view = self._view(self.rows[iid])
            states.setdefault(view["state"], []).append(view)
        return {
            "batch": batch,
            "total": len(ids),
            "counts": {state: len(rows) for state, rows in states.items()},
            "states": states,
        }

    @staticmethod
    def _view(case):
        return {"id": case.id, "owner": case.owner or case.actor,
                "state": case.state, "version": case.version,
                "batch": case.batch, "note": case.note}

    def snapshot(self):
        return [asdict(self.rows[k]) for k in sorted(self.rows)]

    # ---- 持久化 ----
    def _persist(self):
        if self.store is None:
            return
        self.store.save({
            "rows": [asdict(row) for row in self.rows.values()],
            "keys": dict(self.keys),
            "batches": {k: asdict(b) for k, b in self.batches.items()},
        })

    def _load(self, state):
        if not isinstance(state, dict):
            return
        for raw in state.get("rows", []):
            row = Case(**raw)
            self.rows[row.id] = row
        self.keys.update(state.get("keys", {}))
        for key, raw in state.get("batches", {}).items():
            self.batches[key] = Batch(raw["key"], list(raw["items"]),
                                      list(raw["failed"]))
