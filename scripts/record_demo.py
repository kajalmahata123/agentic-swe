"""Record a captioned walkthrough of the oversight dashboard over the recorded live runs.

    pip install playwright && python -m playwright install chromium
    make demo-video            # restores demo-state, serves it, records docs/media/demo.mp4 (+ preview gif)

Everything shown is real recorded state (demo-state/); nothing is mocked for the video.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

AMBIGUOUS = "run_9f7e81be929b"
GREENFIELD = "run_c856232d1346"
BROWNFIELD = "run_2e7440e535cd"
W, H = 1440, 900

OVERLAY_JS = """
(() => {
  if (document.getElementById('demo-cap')) return;
  const st = document.createElement('style');
  st.textContent = `
    #demo-cap{position:fixed;left:50%;bottom:28px;transform:translateX(-50%);max-width:1100px;z-index:99999;
      background:rgba(10,14,24,.88);color:#f4f6fb;font:500 19px/1.45 -apple-system,Segoe UI,Inter,sans-serif;
      padding:14px 22px;border-radius:12px;box-shadow:0 8px 30px rgba(0,0,0,.35);border:1px solid rgba(255,255,255,.12);
      transition:opacity .35s;opacity:0;text-align:center}
    #demo-cap b{color:#7cc4ff}
    #demo-card{position:fixed;inset:0;z-index:100000;display:flex;flex-direction:column;align-items:center;
      justify-content:center;background:radial-gradient(circle at 30% 20%,#1b2a4a,#070b14 70%);color:#fff;
      font-family:-apple-system,Segoe UI,Inter,sans-serif;transition:opacity .5s;text-align:center;padding:40px}
    #demo-card h1{font-size:54px;margin:0 0 14px;letter-spacing:-.5px}
    #demo-card p{font-size:23px;color:#b9c6e4;margin:6px 0;max-width:1000px}
    #demo-card code{background:rgba(255,255,255,.08);padding:3px 10px;border-radius:6px;color:#9fe2a5}
    .demo-cursor{position:fixed;width:22px;height:22px;border-radius:50%;background:rgba(124,196,255,.45);
      border:2px solid #7cc4ff;z-index:99998;pointer-events:none;transform:translate(-50%,-50%);transition:all .45s}`;
  document.head.appendChild(st);
  const cap = document.createElement('div'); cap.id = 'demo-cap'; document.body.appendChild(cap);
  const cur = document.createElement('div'); cur.className = 'demo-cursor'; cur.style.left='-50px';
  document.body.appendChild(cur);
})();
"""


class Tour:
    def __init__(self, page: Page, base: str, pace: float, narrator=None) -> None:
        self.page, self.base, self.pace = page, base.rstrip("/"), pace
        self.narrator = narrator
        self.clips: list[tuple[float, Path]] = []  # (offset in video seconds, wav)
        self.t0 = time.time()

    def wait(self, s: float) -> None:
        time.sleep(s * self.pace)

    def overlay(self) -> None:
        self.page.evaluate(OVERLAY_JS)

    def _speak(self, say: str | None, lead: float = 0.25) -> float:
        """Queue narration starting with the caption; return how long the scene must hold for it."""
        if not (self.narrator and say):
            return 0.0
        wav, dur = self.narrator.clip(say)
        self.clips.append((time.time() - self.t0 + lead, wav))
        return dur + lead + 0.6

    def caption(self, html: str, hold: float = 4.0, say: str | None = None) -> None:
        self.overlay()
        need = self._speak(say)
        self.page.evaluate("""(h) => { const c = document.getElementById('demo-cap');
            c.style.opacity = 0; setTimeout(() => { c.innerHTML = h; c.style.opacity = 1; }, 250); }""", html)
        self.wait(max(hold, need / self.pace if self.pace else hold))

    def card(self, title: str, lines: list[str], hold: float = 4.5, say: str | None = None) -> None:
        self.overlay()
        need = self._speak(say, lead=0.1)
        self.page.evaluate("""([t, ls]) => { let c = document.getElementById('demo-card');
            if (!c) { c = document.createElement('div'); c.id = 'demo-card'; document.body.appendChild(c); }
            c.style.opacity = 1; c.innerHTML = '<h1>' + t + '</h1>' + ls.map(l => '<p>' + l + '</p>').join(''); }""",
                           [title, lines])
        self.wait(max(hold, need / self.pace if self.pace else hold))
        self.page.evaluate("() => { const c = document.getElementById('demo-card'); if (c) { c.style.opacity = 0;"
                           " setTimeout(() => c.remove(), 600); } }")
        self.wait(0.7)

    def go(self, run: str, tab: str, settle: float = 2.0) -> None:
        self.page.goto(f"{self.base}/#/runs/{run}/{tab}")
        self.page.wait_for_load_state("networkidle")
        self.wait(settle)
        self.overlay()

    def point(self, selector: str) -> None:
        box = self.page.locator(selector).first.bounding_box()
        if box:
            self.page.evaluate("([x, y]) => { const c = document.querySelector('.demo-cursor');"
                               " c.style.left = x + 'px'; c.style.top = y + 'px'; }",
                               [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2])
            self.wait(0.6)

    def click(self, selector: str, settle: float = 1.5) -> None:
        self.point(selector)
        self.page.locator(selector).first.click()
        self.wait(settle)

    def scroll(self, selector: str, dy: int, steps: int = 4) -> None:
        for _ in range(steps):
            self.page.evaluate("([s, d]) => { const el = document.querySelector(s) || document.scrollingElement;"
                               " el.scrollBy({top: d, behavior: 'smooth'}); }", [selector, dy // steps])
            self.wait(0.5)


def run_tour(t: Tour) -> None:
    p = t.page
    p.goto(t.base + "/")
    p.wait_for_load_state("networkidle")
    t.card("Agentic SWE",
           ["A governed multi-agent SDLC orchestrator: requirement → design → parallel build → tests →",
            "security review → human-approved release.  Demo product: a URL shortener built by the agents.",
            "Everything below is <b>recorded state from three live runs</b> (Claude Opus 5.5)."], 6,
           say="This is Agentic S W E: a governed, multi-agent system that takes a software requirement all the "
               "way to a reviewed, tested and released service. The demo product is a URL shortener, built "
               "entirely by AI agents. Everything you'll see is real recorded state from three live runs.")
    t.caption("The <b>runs</b> sidebar: greenfield (v1.0.0), brownfield (v1.1.0) and the <b>ambiguous</b> "
              "scenario (v1.2.0), each delivered end-to-end by agents under governance.", 5,
              say="We ran three scenarios: a greenfield build from scratch, a brownfield change to that code, "
                  "and an ambiguous business request. All three were delivered and released.")

    # ---------------------------------------------------------------- pipeline
    t.go(AMBIGUOUS, "pipeline", 2.5)
    fit = 'button:has-text("Fit")'
    if p.locator(fit).count():
        t.click(fit, 1.0)
    t.caption("Ambiguous scenario: <i>“Make the link service faster and more trustworthy”</i>. "
              "The pipeline is an explicit <b>dependency graph</b> with entry/exit gates on every node.", 5.5,
              say="Let's open the ambiguous scenario. Marketing simply asked us to make the link service faster "
                  "and more trustworthy. The orchestrator runs the work as an explicit dependency graph, with "
                  "entry and exit gates on every single node.")
    t.caption("The <b>planner</b> expanded the approved design at runtime into 7 work packages, run in "
              "<b>parallel waves</b> in isolated git worktrees, joined by the <b>integrate</b> barrier.", 5.5,
              say="After the design was approved, the planner expanded the graph at runtime into seven work "
                  "packages. They run in parallel waves, each agent in its own isolated git worktree, and an "
                  "integration barrier joins them back together.")
    t.click('g.node[data-node="clarify"]', 1.2)
    t.caption("<b>Human checkpoint</b>: the requirements agent raised 2 blocking questions (load targets, "
              "database). The product owner's answers <b>re-planned</b> the requirements.", 6,
              say="Because the request was vague, the requirements agent raised two blocking questions: the "
                  "load targets, and the production database. A human product owner answered them, and the "
                  "requirements were automatically regenerated from those answers.")
    t.click('g.node[data-node="design"]', 1.2)
    t.caption("Every node shows its spec, attempts, <b>gate results</b>, merged diff and the full "
              "<b>agent transcript</b>. The design needed a human approval bound to its content hash.", 6,
              say="Every node can be inspected: its specification, every attempt, the gate results, the merged "
                  "diff, and the full agent transcript. The design itself needed a human approval, bound to the "
                  "exact content that was reviewed.")
    t.scroll(".node-panel", 900, 5)
    t.wait(1)
    t.click('g.node[data-node="repair-1"]', 1.2)
    t.caption("Verification failed on 3 tests → the engine inserted a <b>repair loop</b> at runtime "
              "(repair-1 → verify-2), instead of blindly retrying. 404/404 tests pass afterwards.", 6,
              say="When verification failed on three tests, the engine didn't blindly retry. It inserted a "
                  "repair loop into the graph at runtime: a repair agent, followed by a fresh verification. "
                  "Afterwards, all four hundred and four tests passed.")
    t.click('g.node[data-node="impl:WP-1"]', 1.5)
    t.scroll(".node-panel", 1400, 5)
    t.caption("Each work package merged as a reviewable <b>diff</b>; failed attempts are rolled back exactly "
              "by discarding their git worktree.", 5,
              say="Each work package lands as a reviewable diff. And if an attempt fails, it's rolled back "
                  "exactly, simply by discarding its worktree.")

    # --------------------------------------------------------------- approvals
    t.go(AMBIGUOUS, "approvals", 2)
    t.caption("<b>Approvals</b>: clarification answers, design approvals, a delegated approval (recorded "
              "as such), and the <b>release sign-off</b>, which policy never lets an agent make.", 6,
              say="The approvals tab is the human control panel: clarification answers, design approvals, "
                  "delegated approvals that are clearly recorded as such, and the release sign-off, which "
                  "policy never allows an agent to make.")
    t.scroll(".tabpanel", 1800, 6)

    # ---------------------------------------------------------------- timeline
    t.go(AMBIGUOUS, "timeline", 2)
    t.caption("The <b>audit trail</b>: 778 events: every tool call decision, gate, approval, re-plan and "
              "amendment, <b>hash-chained</b> so any tampering is detectable.", 5.5,
              say="Everything is recorded in an audit trail: seven hundred and seventy-eight events for this "
                  "run. Every tool call decision, gate, approval and re-plan, chained together with hashes, "
                  "so any tampering can be detected.")
    btn = 'button:has-text("Verify audit chain")'
    if p.locator(btn).count():
        t.click(btn, 2.5)
        t.caption("Verifying the chain: <b>intact</b>.", 3.5, say="Let's verify it. The chain is intact.")
    search = 'input[placeholder^="Filter by node"]'
    if p.locator(search).count():
        t.point(search)
        p.locator(search).first.type("amend", delay=120)
        t.wait(1.5)
    t.caption("Filtered to the <b>amendment</b> (campaign links must expire) arriving mid-run → invalidation → "
              "regenerated requirements, impact analysis and a fresh design approval.", 5.5,
              say="Midway through the run, the product owner amended the requirement: campaign links must "
                  "expire automatically when the campaign ends.")
    if p.locator(search).count():
        p.locator(search).first.fill("replan")
        t.wait(1.5)
        t.caption("…and the <b>re-plan</b> events it triggered: only nodes whose inputs actually changed re-run.", 4.5,
                  say="That triggered these re-plan events. The orchestrator invalidated only the work whose "
                      "inputs actually changed, regenerated it, and asked a human to approve the new design.")

    # --------------------------------------------------------------- artifacts
    t.go(AMBIGUOUS, "artifacts", 2)
    t.caption("<b>Artifacts</b> are versioned and content-hashed with lineage: <i>requirement v3</i> = "
              "original brief + clarification answers + amendment.", 6,
              say="Stages only communicate through versioned, content-hashed artifacts, with full lineage.")
    t.scroll(".tabpanel", 1200, 4)
    group = 'details.art-group:has(span.art-name:text-is("requirement"))'
    if p.locator(group).count():
        t.click(f"{group} summary", 1.0)
        t.click(f"{group} button.art-ver", 2.0)
        t.caption("The amended brief: its value, hash, producer and what it was <b>derived from</b>.", 4.5,
                  say="Here is version three of the requirement: the original brief, plus the clarification "
                      "answers, plus the amendment.")
        t.scroll(".art-viewer", 900, 4)

    # ----------------------------------------------------------------- metrics
    t.go(AMBIGUOUS, "metrics", 2.5)
    t.caption("<b>Reliability metrics</b> derived from the audit trail: success rate, retries, rollbacks, "
              "MTTR, re-plans, repair loops, approval wait, policy denials, latency and LLM cost.", 6.5,
              say="Reliability metrics are computed straight from the audit trail: success rate, retries, "
                  "rollbacks, mean time to recovery, re-plans, repair loops, approval wait times, latency, "
                  "and cost.")
    t.scroll(".tabpanel", 900, 3)

    # ------------------------------------------------------------------ report
    t.go(AMBIGUOUS, "report", 2)
    t.caption("A <b>final engineering summary</b> is generated per run: plan, decomposition, ADRs, "
              "validation, risks, assumptions, human oversight, metrics, lineage.", 5.5,
              say="Every run produces a final engineering summary: the plan and decomposition, architecture "
                  "decisions, validation results, risks, assumptions, human oversight, and lineage.")
    t.scroll(".tabpanel", 3000, 8)

    # ------------------------------------------------------------------- files
    t.go(AMBIGUOUS, "files", 2.5)
    t.caption("<b>Files</b>: the generated, git-versioned service itself: layered FastAPI app, tests, "
              "docs, runbook.", 4.5,
              say="And this is the product itself: a layered FastAPI service with its tests, documentation "
                  "and runbook, all written by the agents.")
    f = 'text="app/main.py"'
    if p.locator(f).count():
        t.click(f, 2)
        t.wait(2)

    # -------------------------------------------------------------- greenfield
    t.go(GREENFIELD, "pipeline", 2.5)
    t.caption("Greenfield: the security reviewer found unauthenticated mutating endpoints (high) → "
              "<b>repair loop</b> added API-key auth → re-verified (165 tests, 99% coverage) → released v1.0.0.",
              6.5,
              say="In the greenfield run, the security reviewer flagged unauthenticated endpoints as high risk. "
                  "A repair loop added API key authentication, everything was re-verified, and version one was "
                  "released.")
    t.go(BROWNFIELD, "pipeline", 2.5)
    t.caption("Brownfield: <b>codebase scan + impact analysis</b> on the v1.0.0 code found 18 impacted files, "
              "concluded bug BUG-77 was not reproducible, and recovered from real network drops.", 6.5,
              say="In the brownfield run, a codebase scan and impact analysis mapped eighteen impacted files, "
                  "investigated a reported bug and found it wasn't reproducible, and recovered automatically "
                  "from real network failures.")
    t.card("Try it yourself", [
        "<code>git clone https://github.com/kajalmahata123/agentic-swe</code>",
        "<code>make setup &amp;&amp; make demo</code>  ·  explore these exact runs, no API key",
        "<code>sdlc run scenarios/greenfield.yaml</code>  ·  run it live with your Anthropic key"], 6,
        say="Agents execute, the system verifies, and humans stay in control. Clone the repository and run "
            "make demo to explore these exact runs yourself.")


def record(url: str, pace: float, theme: str, narrator, out_webm: Path) -> list[tuple[float, Path]]:
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": W, "height": H}, color_scheme=theme,
                                  record_video_dir=tmp, record_video_size={"width": W, "height": H})
        page = ctx.new_page()
        tour = Tour(page, url, pace, narrator)
        run_tour(tour)
        video = Path(page.video.path())
        ctx.close()
        browser.close()
        shutil.copy(video, out_webm)
    return tour.clips


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8791")
    ap.add_argument("--out", default="docs/media/demo.mp4")
    ap.add_argument("--gif", default="docs/media/demo-preview.gif")
    ap.add_argument("--pace", type=float, default=1.0, help="multiply all pauses (e.g. 0.3 for a dry run)")
    ap.add_argument("--theme", default="dark", choices=["dark", "light"])
    ap.add_argument("--narrate", action="store_true", help="add a Gemini TTS voice-over (needs GEMINI_API_KEY)")
    args = ap.parse_args()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    narrator = None
    if args.narrate:
        from narration import Narrator

        from sdlc.llm.factory import load_dotenv

        load_dotenv()
        narrator = Narrator(Path(".demo-audio-cache"))
        # Prefetch pass: synthesise + cache every line so the real take never waits on the TTS API.
        with tempfile.TemporaryDirectory() as tmp:
            record(args.url, 0.02, args.theme, narrator, Path(tmp) / "prefetch.webm")
    raw = out.with_suffix(".webm")
    clips = record(args.url, args.pace, args.theme, narrator, raw)
    silent = out.with_name(out.stem + ".silent.mp4")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw), "-c:v", "libx264", "-preset", "slow",
                    "-crf", "26", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(silent)], check=True)
    raw.unlink()
    if narrator and clips:
        from narration import mix_onto

        mix_onto(silent, clips, out)
        silent.unlink()
    else:
        silent.replace(out)
    if args.gif:
        palette = out.with_suffix(".png")
        vf = "fps=8,scale=960:-1:flags=lanczos"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "8", "-t", "40", "-i", str(out), "-vf",
                        f"{vf},palettegen=max_colors=128", str(palette)], check=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "8", "-t", "40", "-i", str(out), "-i",
                        str(palette), "-lavfi", f"{vf}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=4",
                        args.gif], check=True)
        palette.unlink()
    print(f"wrote {out}" + (f" with {len(clips)} narration clips" if narrator else "")
          + (f" and {args.gif}" if args.gif else ""))


if __name__ == "__main__":
    main()
