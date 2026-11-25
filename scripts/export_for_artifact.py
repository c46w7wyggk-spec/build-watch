"""Copy the latest monitor results into a folder the Build Watch artifact can publish.

Usage: python scripts/export_for_artifact.py OUT_DIR [--keep N]
Writes OUT_DIR/monitor/index.json (trimmed to the last N clear images per site)
and the matching images, then prints a JSON map of published path -> source path
for the Artifact tool's `files` parameter.
"""
import argparse
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

ap = argparse.ArgumentParser()
ap.add_argument("out")
ap.add_argument("--keep", type=int, default=10)
a = ap.parse_args()

out = Path(a.out).resolve()
mon = out / "monitor"
if mon.exists():
    shutil.rmtree(mon)
mon.mkdir(parents=True)

ix = json.loads((ROOT / "data" / "index.json").read_text())
files = {}
for sid, rec in ix["sites"].items():
    usable = [o for o in rec["observations"] if o.get("usable")][-a.keep:]
    rec["observations"] = usable
    for o in usable:
        for key in ("image", "diff"):
            if o.get(key):
                src = ROOT / o[key]
                dst = mon / o[key]
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                files["monitor/" + o[key]] = str(dst)
(mon / "index.json").write_text(json.dumps(ix))
files["monitor/index.json"] = str(mon / "index.json")
print(json.dumps(files))
