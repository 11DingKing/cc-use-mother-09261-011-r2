"""SQLite 持久层：条目、批次导入报告、单条幂等键三张关系表。"""
import sqlite3
import json

# 与 workflow.Case 字段保持一致；新增字段走 ALTER 兼容旧库。
ITEM_COLUMNS = [
    ("id", "TEXT PRIMARY KEY"),
    ("actor", "TEXT NOT NULL DEFAULT ''"),
    ("state", "TEXT NOT NULL"),
    ("version", "INTEGER NOT NULL DEFAULT 1"),
    ("batch_id", "TEXT NOT NULL DEFAULT ''"),
    ("owner", "TEXT NOT NULL DEFAULT ''"),
    ("content", "TEXT NOT NULL DEFAULT ''"),
    ("reason", "TEXT NOT NULL DEFAULT ''"),
    ("decided_by", "TEXT NOT NULL DEFAULT ''"),
    ("updated_at", "TEXT NOT NULL DEFAULT ''"),
]


class SQLiteStore:
    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        cols = ", ".join("%s %s" % c for c in ITEM_COLUMNS)
        self.db.execute("CREATE TABLE IF NOT EXISTS items (%s)" % cols)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS batches ("
            "batch_id TEXT PRIMARY KEY, report TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT '')")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS idem_keys (key TEXT PRIMARY KEY, case_id TEXT NOT NULL)")
        self._migrate()
        self.db.commit()

    def _migrate(self):
        existing = {r[1] for r in self.db.execute("PRAGMA table_info(items)")}
        for name, decl in ITEM_COLUMNS:
            if name not in existing:
                self.db.execute("ALTER TABLE items ADD COLUMN %s %s" % (name, decl))

    # ---- 条目 ----
    def put_item(self, case):
        names = [c[0] for c in ITEM_COLUMNS]
        placeholders = ", ".join("?" for _ in names)
        updates = ", ".join("%s=excluded.%s" % (n, n) for n in names if n != "id")
        self.db.execute(
            "INSERT INTO items(%s) VALUES(%s) ON CONFLICT(id) DO UPDATE SET %s"
            % (", ".join(names), placeholders, updates),
            tuple(case.get(n, "") for n in names))
        self.db.commit()

    def list_items(self):
        names = [c[0] for c in ITEM_COLUMNS]
        rows = self.db.execute("SELECT %s FROM items" % ", ".join(names)).fetchall()
        return [dict(zip(names, row)) for row in rows]

    # ---- 批次 ----
    def put_batch(self, batch_id, report, created_at=""):
        self.db.execute(
            "INSERT INTO batches(batch_id, report, created_at) VALUES(?, ?, ?) "
            "ON CONFLICT(batch_id) DO NOTHING",
            (batch_id, report, created_at))
        self.db.commit()

    def list_batches(self):
        return self.db.execute("SELECT batch_id, report FROM batches").fetchall()

    # ---- 单条幂等键 ----
    def put_key(self, key, case_id):
        self.db.execute(
            "INSERT INTO idem_keys(key, case_id) VALUES(?, ?) ON CONFLICT(key) DO NOTHING",
            (key, case_id))
        self.db.commit()

    def list_keys(self):
        return self.db.execute("SELECT key, case_id FROM idem_keys").fetchall()

    # ---- 旧接口（整包快照，兼容历史调用方）----
    def save(self, value):
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS snapshots(id INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT NOT NULL)")
        self.db.execute("INSERT INTO snapshots(body) VALUES(?)",
                        (json.dumps(value, ensure_ascii=False),))
        self.db.commit()

    def latest(self):
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS snapshots(id INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT NOT NULL)")
        row = self.db.execute("SELECT body FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
        return json.loads(row[0]) if row else []
