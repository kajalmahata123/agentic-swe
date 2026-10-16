"""Campaign load profile on one instance with a WAL temp-file SQLite DB.

NFR-10: paced ~50 RPS for 20 s (95 % redirects / 5 % creates), redirect p95 < 100 ms,
zero 5xx, clicks == successful GETs minus logged best-effort failures.
NFR-19: unpaced burst of 2,000 requests from 16 threads, zero 5xx, redirect
p95 < 250 ms, clicks + logged failures == successful GETs.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest
from app.models import ClickEvent, Link
from helpers import count_rows, new_session

N_LINKS = 1_000


def _p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[max(int(len(ordered) * 0.95) - 1, 0)]


def _seed(client) -> list[str]:
    codes = [f"Camp{i:06d}" for i in range(N_LINKS)]
    with new_session(client) as s:
        s.add_all(
            Link(
                code=c,
                url=f"https://example.com/campaign/{c}?src=tv",
                # Every third link carries a future campaign end.
                expires_at=datetime(2099, 12, 31, 23, 59) if i % 3 == 0 else None,
            )
            for i, c in enumerate(codes)
        )
        s.commit()
    return codes


class _Recorder:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.statuses: Counter[tuple[str, int]] = Counter()
        self.redirect_ms: list[float] = []

    def run(self, client, i: int, codes: list[str]) -> None:
        if i % 20 == 0:
            kind = "post"
            start = time.perf_counter()
            resp = client.post("/api/links", json={"url": f"https://example.com/new/{i}"})
        else:
            kind = "get"
            code = codes[(i * 7919) % len(codes)]
            start = time.perf_counter()
            resp = client.get(f"/{code}", follow_redirects=False)
        elapsed = (time.perf_counter() - start) * 1000
        with self.lock:
            self.statuses[(kind, resp.status_code)] += 1
            if kind == "get":
                self.redirect_ms.append(elapsed)


def _failures(caplog) -> int:
    return sum(
        1 for r in caplog.records
        if r.name == "app.services.links" and "failed to record click" in r.getMessage()
    )


def _assert_no_5xx(rec: _Recorder) -> None:
    assert not [k for k in rec.statuses if k[1] >= 500], dict(rec.statuses)
    assert set(rec.statuses) <= {("get", 307), ("post", 201)}, dict(rec.statuses)


@pytest.mark.slow
def test_paced_50_rps_mixed_load(client, caplog):
    """Covers: NFR-10, NFR-11, FR-15, FR-16"""
    codes = _seed(client)
    for c in codes[:20]:
        client.head(f"/{c}", follow_redirects=False)  # warm-up, no clicks
    rate, duration = 50, 20
    total = rate * duration
    rec = _Recorder()
    with caplog.at_level(logging.WARNING, logger="app"):
        with ThreadPoolExecutor(max_workers=16) as pool:
            t0 = time.perf_counter()
            futures = []
            for i in range(total):
                due = t0 + i / rate
                delay = due - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
                futures.append(pool.submit(rec.run, client, i, codes))
            for f in futures:
                f.result()
        wall = time.perf_counter() - t0
    _assert_no_5xx(rec)
    gets = rec.statuses[("get", 307)]
    posts = rec.statuses[("post", 201)]
    assert gets + posts == total
    assert posts == total // 20
    # Open-loop pacing held (the generator did not fall far behind schedule).
    assert wall < duration + 5, wall
    p95 = _p95(rec.redirect_ms)
    assert p95 < 100, p95
    failures = _failures(caplog)
    assert failures == 0
    assert count_rows(client, ClickEvent) == gets - failures


@pytest.mark.slow
def test_unpaced_burst_16_threads(client, caplog):
    """Covers: NFR-19, NFR-12, FR-15"""
    codes = _seed(client)
    total = 2_000
    rec = _Recorder()
    with caplog.at_level(logging.WARNING, logger="app"):
        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(lambda i: rec.run(client, i, codes), range(total)))
    _assert_no_5xx(rec)
    gets = rec.statuses[("get", 307)]
    assert gets + rec.statuses[("post", 201)] == total
    p95 = _p95(rec.redirect_ms)
    assert p95 < 250, p95
    assert count_rows(client, ClickEvent) + _failures(caplog) == gets
