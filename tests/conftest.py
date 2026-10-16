from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import pytest

from sdlc.domain.models import ApprovalStatus
from sdlc.governance.approvals import ApprovalService
from sdlc.governance.audit import AuditLog
from sdlc.governance.policy import Policy
from sdlc.persistence.store import Store


@pytest.fixture
def sdlc_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SDLC_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


@pytest.fixture
def store(tmp_path) -> Iterator[Store]:
    s = Store(tmp_path / "state.db")
    yield s
    s.close()


@pytest.fixture
def policy() -> Policy:
    return Policy.load()


@pytest.fixture
def audit(store) -> AuditLog:
    return AuditLog(store)


class HumanBot:
    """Simulates a human on the dashboard: approves whatever is pending (optionally rejecting once)."""

    def __init__(self, store: Store, policy: Policy, reject_first: set[str] | None = None) -> None:
        self.service = ApprovalService(store, AuditLog(store), policy)
        self.store = store
        self.reject_first = set(reject_first or ())
        self._stop = threading.Event()
        self.decided: list[tuple[str, str]] = []
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            for a in self.store.list_approvals(status=ApprovalStatus.PENDING):
                if a.kind.value in self.reject_first:
                    self.reject_first.discard(a.kind.value)
                    self.service.decide(a.id, False, "human:reviewer", "Add an ADR about data retention.")
                    self.decided.append((a.kind.value, "rejected"))
                else:
                    self.service.decide(a.id, True, "human:reviewer", "looks good")
                    self.decided.append((a.kind.value, "approved"))
            time.sleep(0.05)

    def __enter__(self) -> HumanBot:
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self.thread.join(timeout=5)
