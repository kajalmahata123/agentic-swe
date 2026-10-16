"""Audit-grade, tamper-evident event log.

Every state transition, tool call, policy decision, approval and gate result is an event.
Events are hash-chained per run: hash_n = sha256(prev_hash || canonical(event_n)). Editing
or deleting any historic row breaks the chain, which `verify()` detects. Appends take an
IMMEDIATE transaction so the engine and dashboard processes can both write safely.
"""

from __future__ import annotations

import hashlib
import json
import sys
from typing import Any

from sdlc.domain.models import now
from sdlc.persistence.store import Store

GENESIS = "0" * 64


def _canonical(
    run_id: str, seq: int, ts: float, type_: str, node_id: str | None, attempt: int | None, data: str
) -> str:
    return json.dumps([run_id, seq, round(ts, 6), type_, node_id, attempt, data], separators=(",", ":"))


class AuditLog:
    def __init__(self, store: Store, echo: bool = False) -> None:
        self.store = store
        self.echo = echo
        self._listeners: list = []

    def subscribe(self, fn) -> None:
        """Register an in-process listener (e.g. the CLI live printer)."""
        self._listeners.append(fn)

    def record(
        self, run_id: str, type_: str, node_id: str | None = None, attempt: int | None = None, **data: Any
    ) -> dict[str, Any]:
        payload = json.dumps(data, default=str, sort_keys=True)
        ts = now()
        with self.store._lock:
            conn = self.store._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                last = conn.execute(
                    "SELECT seq, hash FROM events WHERE run_id=? ORDER BY seq DESC LIMIT 1", (run_id,)
                ).fetchone()
                seq = (last["seq"] + 1) if last else 1
                prev = last["hash"] if last else GENESIS
                digest = hashlib.sha256(
                    (prev + _canonical(run_id, seq, ts, type_, node_id, attempt, payload)).encode()
                ).hexdigest()
                conn.execute(
                    "INSERT INTO events (run_id, seq, ts, type, node_id, attempt, data, prev_hash, hash) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (run_id, seq, ts, type_, node_id, attempt, payload, prev, digest),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        event = {
            "run_id": run_id,
            "seq": seq,
            "ts": ts,
            "type": type_,
            "node_id": node_id,
            "attempt": attempt,
            "data": data,
            "hash": digest,
        }
        for fn in self._listeners:
            try:
                fn(event)
            except Exception as exc:  # a broken listener must never break the audit trail
                print(f"[audit] listener error: {exc}", file=sys.stderr)
        return event

    def verify(self, run_id: str) -> tuple[bool, str]:
        """Recompute the chain. Returns (ok, message)."""
        prev = GENESIS
        expected_seq = 1
        with self.store._lock:
            rows = self.store._conn.execute(
                "SELECT * FROM events WHERE run_id=? ORDER BY seq", (run_id,)
            ).fetchall()
        for r in rows:
            if r["seq"] != expected_seq:
                return False, f"gap in sequence: expected {expected_seq}, found {r['seq']}"
            if r["prev_hash"] != prev:
                return False, f"event {r['seq']}: prev_hash mismatch"
            digest = hashlib.sha256(
                (
                    prev
                    + _canonical(
                        r["run_id"], r["seq"], r["ts"], r["type"], r["node_id"], r["attempt"], r["data"]
                    )
                ).encode()
            ).hexdigest()
            if digest != r["hash"]:
                return False, f"event {r['seq']} ({r['type']}): content hash mismatch — log was altered"
            prev = digest
            expected_seq += 1
        return True, f"chain intact: {len(rows)} events, head {prev[:12]}"
