"""Print the event timeline of the most recent run in an SDLC_HOME (developer aid)."""
import sys
from sdlc.engine.bootstrap import open_store

store = open_store()
run = store.list_runs(1)[0]
print(run["id"], run["status"], run["error"])
for e in store.events(run["id"]):
    if e["type"] in ("agent.turn", "artifact.stored", "tool.guard"):
        continue
    d = e["data"]
    detail = d.get("error") or d.get("detail") or d.get("reason") or d.get("summary") or ""
    if e["type"] == "gate.result" and d.get("passed"):
        continue
    print(f"{e['seq']:4} {e['type']:24} {str(e['node_id']):18} a{e['attempt']} {str(detail)[:300]}")
