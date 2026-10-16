"""SQLite-backed state store.

One file holds every run's graph, node states, artifacts, decisions, approvals,
amendments and the hash-chained audit log. WAL mode lets the CLI engine process and the
dashboard process share it concurrently: the dashboard writes approvals / stop requests /
amendments, the engine polls for them. All state needed to resume a run lives here.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from sdlc.domain.models import (
    Approval,
    ApprovalKind,
    ApprovalStatus,
    Artifact,
    Decision,
    NodeState,
    NodeStatus,
    RunStatus,
    new_id,
    now,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    scenario TEXT NOT NULL,
    requirement TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    config TEXT NOT NULL,
    graph TEXT NOT NULL DEFAULT '[]',
    workspace TEXT,
    stop_requested INTEGER NOT NULL DEFAULT 0,
    stop_reason TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS node_states (
    run_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    state TEXT NOT NULL,
    PRIMARY KEY (run_id, node_id)
);
CREATE TABLE IF NOT EXISTS artifacts (
    run_id TEXT NOT NULL,
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    hash TEXT NOT NULL,
    producer TEXT NOT NULL,
    value TEXT NOT NULL,
    derived_from TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (run_id, name, version)
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    ts REAL NOT NULL,
    type TEXT NOT NULL,
    node_id TEXT,
    attempt INTEGER,
    data TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS amendments (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    text TEXT NOT NULL,
    author TEXT NOT NULL,
    created_at REAL NOT NULL,
    applied INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_events_run_type ON events(run_id, type);
CREATE INDEX IF NOT EXISTS ix_approvals_status ON approvals(status);
"""


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def close(self) -> None:
        self._conn.close()

    # --------------------------------------------------------------------- runs
    def create_run(self, scenario: str, requirement: str, config: dict[str, Any]) -> str:
        run_id = new_id("run")
        with self.tx() as c:
            c.execute(
                "INSERT INTO runs (id, scenario, requirement, status, created_at, config) "
                "VALUES (?,?,?,?,?,?)",
                (run_id, scenario, requirement, RunStatus.PENDING.value, now(), json.dumps(config)),
            )
        return run_id

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["config"] = json.loads(d["config"])
        d["graph"] = json.loads(d["graph"])
        return d

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, scenario, requirement, status, created_at, started_at, finished_at, "
                "stop_reason, error, workspace FROM runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def update_run(self, run_id: str, **fields: Any) -> None:
        if not fields:
            return
        for key in ("config", "graph"):
            if key in fields and not isinstance(fields[key], str):
                fields[key] = json.dumps(fields[key])
        if "status" in fields and isinstance(fields["status"], RunStatus):
            fields["status"] = fields["status"].value
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE runs SET {cols} WHERE id=?", (*fields.values(), run_id))

    def request_stop(self, run_id: str, reason: str) -> None:
        self.update_run(run_id, stop_requested=1, stop_reason=reason)

    def clear_stop(self, run_id: str) -> None:
        self.update_run(run_id, stop_requested=0, stop_reason=None)

    def stop_requested(self, run_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT stop_requested, stop_reason FROM runs WHERE id=?", (run_id,)
            ).fetchone()
        if row and row["stop_requested"]:
            return row["stop_reason"] or "operator requested stop"
        return None

    # -------------------------------------------------------------- node states
    def save_node_state(self, run_id: str, state: NodeState) -> None:
        data = asdict(state)
        data["status"] = state.status.value
        with self.tx() as c:
            c.execute(
                "INSERT INTO node_states (run_id, node_id, state) VALUES (?,?,?) "
                "ON CONFLICT(run_id, node_id) DO UPDATE SET state=excluded.state",
                (run_id, state.node_id, json.dumps(data)),
            )

    def load_node_states(self, run_id: str) -> dict[str, NodeState]:
        with self._lock:
            rows = self._conn.execute("SELECT state FROM node_states WHERE run_id=?", (run_id,)).fetchall()
        out: dict[str, NodeState] = {}
        for r in rows:
            d = json.loads(r["state"])
            d["status"] = NodeStatus(d["status"])
            out[d["node_id"]] = NodeState(**d)
        return out

    def delete_node_state(self, run_id: str, node_id: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM node_states WHERE run_id=? AND node_id=?", (run_id, node_id))

    # ---------------------------------------------------------------- artifacts
    def put_artifact(self, run_id: str, art: Artifact) -> Artifact:
        with self.tx() as c:
            row = c.execute(
                "SELECT MAX(version) AS v, (SELECT hash FROM artifacts WHERE run_id=? AND name=? "
                "ORDER BY version DESC LIMIT 1) AS h FROM artifacts WHERE run_id=? AND name=?",
                (run_id, art.name, run_id, art.name),
            ).fetchone()
            if row["h"] == art.hash:  # identical content: no new version
                art.version = row["v"]
                return art
            art.version = (row["v"] or 0) + 1
            c.execute(
                "INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    art.name,
                    art.version,
                    art.hash,
                    art.producer,
                    json.dumps(art.value, default=str),
                    json.dumps(art.derived_from),
                    art.created_at,
                ),
            )
        return art

    def _row_to_artifact(self, r: sqlite3.Row) -> Artifact:
        return Artifact(
            name=r["name"],
            value=json.loads(r["value"]),
            producer=r["producer"],
            version=r["version"],
            hash=r["hash"],
            derived_from=json.loads(r["derived_from"]),
            created_at=r["created_at"],
        )

    def latest_artifact(self, run_id: str, name: str) -> Artifact | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM artifacts WHERE run_id=? AND name=? ORDER BY version DESC LIMIT 1",
                (run_id, name),
            ).fetchone()
        return self._row_to_artifact(r) if r else None

    def artifact_by_hash(self, run_id: str, name: str, h: str) -> Artifact | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM artifacts WHERE run_id=? AND name=? AND hash=? ORDER BY version DESC LIMIT 1",
                (run_id, name, h),
            ).fetchone()
        return self._row_to_artifact(r) if r else None

    def list_artifacts(self, run_id: str, latest_only: bool = True) -> list[Artifact]:
        sql = "SELECT * FROM artifacts WHERE run_id=? ORDER BY name, version"
        with self._lock:
            rows = self._conn.execute(sql, (run_id,)).fetchall()
        arts = [self._row_to_artifact(r) for r in rows]
        if latest_only:
            latest: dict[str, Artifact] = {}
            for a in arts:
                latest[a.name] = a
            return list(latest.values())
        return arts

    # ---------------------------------------------------------------- decisions
    def add_decision(self, run_id: str, d: Decision) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO decisions (run_id, node_id, data) VALUES (?,?,?)",
                (run_id, d.node_id, json.dumps(asdict(d))),
            )

    def list_decisions(self, run_id: str) -> list[Decision]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM decisions WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()
        return [Decision(**json.loads(r["data"])) for r in rows]

    # ---------------------------------------------------------------- approvals
    def save_approval(self, a: Approval) -> None:
        data = asdict(a)
        data["kind"] = a.kind.value
        data["status"] = a.status.value
        with self.tx() as c:
            c.execute(
                "INSERT INTO approvals (id, run_id, node_id, kind, status, data) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET status=excluded.status, data=excluded.data",
                (a.id, a.run_id, a.node_id, a.kind.value, a.status.value, json.dumps(data, default=str)),
            )

    @staticmethod
    def _row_to_approval(r: sqlite3.Row) -> Approval:
        d = json.loads(r["data"])
        d["kind"] = ApprovalKind(d["kind"])
        d["status"] = ApprovalStatus(d["status"])
        return Approval(**d)

    def get_approval(self, approval_id: str) -> Approval | None:
        with self._lock:
            r = self._conn.execute("SELECT data FROM approvals WHERE id=?", (approval_id,)).fetchone()
        return self._row_to_approval(r) if r else None

    def list_approvals(
        self, run_id: str | None = None, status: ApprovalStatus | None = None
    ) -> list[Approval]:
        sql, args = "SELECT data FROM approvals WHERE 1=1", []
        if run_id:
            sql += " AND run_id=?"
            args.append(run_id)
        if status:
            sql += " AND status=?"
            args.append(status.value)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return sorted((self._row_to_approval(r) for r in rows), key=lambda a: a.requested_at)

    # --------------------------------------------------------------- amendments
    def add_amendment(self, run_id: str, text: str, author: str) -> str:
        aid = new_id("amd")
        with self.tx() as c:
            c.execute(
                "INSERT INTO amendments (id, run_id, text, author, created_at) VALUES (?,?,?,?,?)",
                (aid, run_id, text, author, now()),
            )
        return aid

    def pending_amendments(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM amendments WHERE run_id=? AND applied=0 ORDER BY created_at", (run_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def all_amendments(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM amendments WHERE run_id=? ORDER BY created_at", (run_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_amendment_applied(self, amendment_id: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE amendments SET applied=1 WHERE id=?", (amendment_id,))

    # ------------------------------------------------------------------- events
    def append_event_row(self, row: dict[str, Any]) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO events (run_id, seq, ts, type, node_id, attempt, data, prev_hash, hash) "
                "VALUES (:run_id, :seq, :ts, :type, :node_id, :attempt, :data, :prev_hash, :hash)",
                row,
            )

    def last_event(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT seq, hash FROM events WHERE run_id=? ORDER BY seq DESC LIMIT 1", (run_id,)
            ).fetchone()
        return dict(r) if r else None

    def events(
        self, run_id: str, after_seq: int = 0, types: list[str] | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM events WHERE run_id=? AND seq>?", [run_id, after_seq]
        if types:
            sql += f" AND type IN ({','.join('?' * len(types))})"
            args.extend(types)
        sql += " ORDER BY seq"
        if limit:
            sql += " LIMIT ?"
            args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["data"] = json.loads(d["data"])
            out.append(d)
        return out
