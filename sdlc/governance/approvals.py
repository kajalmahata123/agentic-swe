"""Human-in-the-loop approval checkpoints.

An approval is bound to the *hash* of the content it covers. If re-planning changes that
content, pending approvals are superseded and a fresh one is requested — a human can never
accidentally approve something they did not see.

Modes:
  dashboard -> wait for a decision written by the dashboard / `sdlc approve` (any process)
  console   -> prompt on this terminal (falls back to waiting if stdin is not a TTY)
  auto      -> auto-approve kinds the policy marks auto-approvable; others still wait for a human
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from collections.abc import Callable
from typing import Any

from sdlc.domain.models import Approval, ApprovalKind, ApprovalStatus, new_id, now
from sdlc.governance.audit import AuditLog
from sdlc.governance.policy import Policy
from sdlc.persistence.store import Store


class StopRequested(Exception):
    """Raised inside waits when the operator (or a safety trigger) asks the run to stop."""


class ApprovalError(Exception):
    pass


_console_lock = threading.Lock()


class ApprovalService:
    def __init__(
        self,
        store: Store,
        audit: AuditLog,
        policy: Policy,
        mode: str = "dashboard",
        operator: str = "operator",
        poll_s: float = 1.0,
    ) -> None:
        self.store = store
        self.audit = audit
        self.policy = policy
        self.mode = mode
        self.operator = operator
        self.poll_s = poll_s

    # ---------------------------------------------------------------- request
    def request(
        self,
        run_id: str,
        node_id: str,
        kind: ApprovalKind,
        summary: str,
        payload: dict[str, Any],
        subject_hash: str,
    ) -> Approval:
        # Re-use an identical decided approval (e.g. on resume) instead of asking twice.
        for a in self.store.list_approvals(run_id):
            if (
                a.node_id == node_id
                and a.kind == kind
                and a.subject_hash == subject_hash
                and a.status in (ApprovalStatus.APPROVED, ApprovalStatus.PENDING)
            ):
                return a
        approval = Approval(
            id=new_id("apr"),
            run_id=run_id,
            node_id=node_id,
            kind=kind,
            summary=summary,
            payload=payload,
            subject_hash=subject_hash,
        )
        self.store.save_approval(approval)
        self.audit.record(
            run_id,
            "approval.requested",
            node_id,
            approval_id=approval.id,
            kind=kind.value,
            summary=summary,
            subject_hash=subject_hash,
        )
        if self.mode == "auto" and self.policy.is_auto_approvable(kind.value):
            response = self._default_response(approval)
            return self.decide(
                approval.id,
                True,
                "policy:auto-approve",
                "auto-approved: kind is auto-approvable under policy.autonomy",
                response,
            )
        return approval

    @staticmethod
    def _default_response(a: Approval) -> dict[str, Any]:
        if a.kind == ApprovalKind.CLARIFICATION:
            return {"answers": {q["id"]: q.get("default") for q in a.payload.get("questions", [])}}
        if a.kind == ApprovalKind.ESCALATION:
            return {"action": "retry"}
        return {}

    # ----------------------------------------------------------------- decide
    def decide(
        self,
        approval_id: str,
        approve: bool,
        actor: str,
        comment: str = "",
        response: dict[str, Any] | None = None,
    ) -> Approval:
        a = self.store.get_approval(approval_id)
        if a is None:
            raise ApprovalError(f"unknown approval {approval_id}")
        if a.status != ApprovalStatus.PENDING:
            raise ApprovalError(f"approval {approval_id} is already {a.status.value}")
        if not approve and not comment:
            raise ApprovalError("a rejection must include a comment explaining what to change")
        a.status = ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED
        a.decided_at = now()
        a.decided_by = actor
        a.comment = comment or None
        a.response = response or (self._default_response(a) if approve else {})
        self.store.save_approval(a)
        self.audit.record(
            a.run_id,
            "approval.decided",
            a.node_id,
            approval_id=a.id,
            kind=a.kind.value,
            decision=a.status.value,
            actor=actor,
            comment=comment,
            response=a.response,
            wait_s=round(a.decided_at - a.requested_at, 3),
        )
        return a

    def supersede(self, run_id: str, node_ids: set[str], reason: str) -> None:
        for a in self.store.list_approvals(run_id, ApprovalStatus.PENDING):
            if a.node_id in node_ids:
                a.status = ApprovalStatus.SUPERSEDED
                a.comment = reason
                a.decided_at = now()
                self.store.save_approval(a)
                self.audit.record(run_id, "approval.superseded", a.node_id, approval_id=a.id, reason=reason)

    # ------------------------------------------------------------------- wait
    async def wait(self, approval: Approval, stop_check: Callable[[], str | None]) -> Approval:
        if approval.status != ApprovalStatus.PENDING:
            return approval
        if self.mode == "console" and sys.stdin.isatty():
            return await asyncio.to_thread(self._console_prompt, approval)
        while True:
            current = self.store.get_approval(approval.id)
            if current and current.status != ApprovalStatus.PENDING:
                return current
            reason = stop_check()
            if reason:
                raise StopRequested(reason)
            await asyncio.sleep(self.poll_s)

    # -------------------------------------------------------- console prompts
    def _console_prompt(self, a: Approval) -> Approval:
        with _console_lock:
            current = self.store.get_approval(a.id)
            if current and current.status != ApprovalStatus.PENDING:
                return current  # decided elsewhere (dashboard) while we queued
            print("\n" + "=" * 78)
            print(f"  HUMAN CHECKPOINT [{a.kind.value}]  node={a.node_id}  id={a.id}")
            print(f"  {a.summary}")
            print("=" * 78)
            if a.kind == ApprovalKind.CLARIFICATION:
                answers = {}
                for q in a.payload.get("questions", []):
                    print(f"\n  ? {q['question']}")
                    if q.get("why"):
                        print(f"    why it matters: {q['why']}")
                    for i, opt in enumerate(q.get("options", []), 1):
                        print(f"    {i}. {opt}")
                    raw = input(f"    answer [default: {q.get('default')}]: ").strip()
                    if raw.isdigit() and q.get("options") and 1 <= int(raw) <= len(q["options"]):
                        raw = q["options"][int(raw) - 1]
                    answers[q["id"]] = raw or q.get("default")
                return self.decide(
                    a.id, True, f"human:{self.operator}", "answered on console", {"answers": answers}
                )
            if a.kind == ApprovalKind.ESCALATION:
                print(json.dumps(a.payload, indent=2, default=str)[:3000])
                choice = ""
                while choice not in {"retry", "skip", "abort"}:
                    choice = input("  action [retry/skip/abort]: ").strip().lower()
                guidance = input("  guidance for the agent (optional): ").strip()
                return self.decide(
                    a.id,
                    choice != "abort",
                    f"human:{self.operator}",
                    guidance or f"operator chose {choice}",
                    {"action": choice, "guidance": guidance},
                )
            print(json.dumps(a.payload, indent=2, default=str)[:4000])
            while True:
                raw = input("  approve? [y/n]: ").strip().lower()
                if raw in {"y", "yes"}:
                    note = input("  note (optional): ").strip()
                    return self.decide(a.id, True, f"human:{self.operator}", note)
                if raw in {"n", "no"}:
                    reason = ""
                    while not reason:
                        reason = input("  what must change? (required): ").strip()
                    return self.decide(a.id, False, f"human:{self.operator}", reason)
