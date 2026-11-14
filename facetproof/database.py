from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3
from .errors import require, storage_errors
from .values import canonical, now, integer

SCHEMA = """
CREATE TABLE IF NOT EXISTS objects (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL,
 revision INTEGER NOT NULL, payload TEXT NOT NULL,
 created REAL NOT NULL, updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS objects_kind ON objects(kind, state);
CREATE TABLE IF NOT EXISTS exposures (
 id TEXT PRIMARY KEY, seal_id TEXT NOT NULL REFERENCES objects(id),
 state TEXT NOT NULL, request TEXT NOT NULL, request_hash TEXT NOT NULL,
 dedup_key TEXT UNIQUE NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
 token TEXT, lease_until REAL, cancel_requested INTEGER NOT NULL DEFAULT 0,
 plate_id TEXT, error TEXT, created REAL NOT NULL, updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS exposure_queue ON exposures(state, created, id);
CREATE TABLE IF NOT EXISTS plates (
 id TEXT PRIMARY KEY, exposure_id TEXT UNIQUE NOT NULL REFERENCES exposures(id),
 state TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
 id TEXT PRIMARY KEY, plate_id TEXT NOT NULL REFERENCES plates(id),
 state TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT NOT NULL,
 action TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL
);
PRAGMA user_version=1;
"""


class Database:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "facetproof.sqlite3"
        conn = self.connect()
        try:
            conn.executescript(SCHEMA)
        finally:
            conn.close()

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    @contextmanager
    def unit_of_work(self):
        conn = self.connect()
        try:
            with storage_errors():
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get(self, conn, obj_id, kind=None):
        require(isinstance(obj_id, str), "reference", "Object reference must be a string")
        row = conn.execute("SELECT * FROM objects WHERE id=?", (obj_id,)).fetchone()
        require(row is not None and (kind is None or row["kind"] == kind),
                "not_found", "Object not found", 404)
        return {**dict(row), "payload": json.loads(row["payload"])}

    def insert(self, conn, obj_id, kind, state, payload):
        stamp = now()
        conn.execute("INSERT INTO objects VALUES (?,?,?,?,?,?,?)",
                     (obj_id, kind, state, 1, canonical(payload), stamp, stamp))
        self.event(conn, obj_id, "created", {"kind": kind, "state": state})
        return self.get(conn, obj_id)

    def update(self, conn, obj, payload, state=None, expected=None):
        revision = obj["revision"] if expected is None else integer(expected, "revision", 1, 2**63-2)
        changed = conn.execute(
            "UPDATE objects SET payload=?,state=?,revision=revision+1,updated=? WHERE id=? AND revision=?",
            (canonical(payload), state or obj["state"], now(), obj["id"], revision)).rowcount
        require(changed == 1, "stale_revision", "Object revision has changed", 409)
        self.event(conn, obj["id"], "revised", {"revision": revision + 1, "state": state or obj["state"]})
        return self.get(conn, obj["id"])

    def event(self, conn, subject, action, payload):
        conn.execute("INSERT INTO events(subject,action,payload,created) VALUES (?,?,?,?)",
                     (subject, action, canonical(payload), now()))

    def list_objects(self, kind):
        with self.unit_of_work() as conn:
            rows = conn.execute("SELECT id FROM objects WHERE kind=? ORDER BY created,id", (kind,)).fetchall()
            return [self.get(conn, row["id"], kind) for row in rows]

    def events(self, subject):
        with self.unit_of_work() as conn:
            rows = conn.execute("SELECT * FROM events WHERE subject=? ORDER BY seq", (subject,)).fetchall()
            return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def backup(self, destination):
        target = Path(destination).resolve()
        require(target != self.path and not target.exists(), "backup", "Destination must be new")
        target.parent.mkdir(parents=True, exist_ok=True)
        source = self.connect()
        dest = sqlite3.connect(target)
        try:
            source.backup(dest)
            require(dest.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
                    "backup", "Backup integrity failed", 500)
        finally:
            source.close()
            dest.close()
        return str(target)
