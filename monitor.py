"""Build Watch monitor.

Every run, for each site in sites.json:
  1. Search the free Sentinel-2 archive (Element 84 Earth Search, no account needed)
     for new, mostly cloud-free scenes over the site.
  2. Cut out a small square around the site (10 m per pixel).
  3. Compare it with the last good image of that site and measure how much changed.
  4. Save the image, a change overlay and the numbers to data/, and flag big changes.

Results land in data/index.json, README.md is regenerated as a dashboard, and
alerts for this run are written to alerts_new.json (the workflow turns them into
GitHub Issues).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
INDEX = DATA / "index.json"
STAC_URL = "https://earth-search.aws.element84.com/v1"
# Collection 1 is the current reprocessed archive; the older collection is kept as a fallback
COLLECTIONS = ["sentinel-2-c1-l2a", "sentinel-2-l2a"]

# Sentinel-2 scene classification (SCL) values
SCL_NODATA = {0, 1}            # no data, saturated/defective
SCL_CLOUD = {3, 8, 9, 10}      # cloud shadow, cloud medium, cloud high, cirrus
SCL_WATER = {6}

# Pixel classes we keep per image: 0 = unusable, 1 = land, 2 = water
UNUSABLE, LAND, WATER = 0, 1, 2

MAX_CLOUD_PCT = 15.0           # skip a crop if more of it than this is cloud/no data
DIFF_THRESHOLD = 45.0          # colour distance (0-255 scale) that counts as "changed"
MIN_VALID_FRACTION = 0.5       # need at least half the crop usable in both images
BACKFILL_SCENES = 6            # clear scenes to collect the first time a site is seen
BACKFILL_DAYS = 240
LOOKBACK_DAYS = 45


# --------------------------------------------------------------------------
# Pure analysis (no network) - unit-tested in tests/test_analysis.py
# --------------------------------------------------------------------------

def classes_from_scl(scl: np.ndarray) -> np.ndarray:
    cls = np.full(scl.shape, LAND, dtype=np.uint8)
    cls[np.isin(scl, list(SCL_WATER))] = WATER
    cls[np.isin(scl, list(SCL_CLOUD | SCL_NODATA))] = UNUSABLE
    return cls


def unusable_pct(cls: np.ndarray, rgb: np.ndarray | None = None) -> float:
    bad = cls == UNUSABLE
    if rgb is not None:
        bad |= rgb.max(axis=2) == 0  # outside the scene footprint
    return float(bad.mean() * 100)


def _neighbour_count(mask: np.ndarray) -> np.ndarray:
    p = np.pad(mask.astype(np.uint8), 1)
    h, w = mask.shape
    out = np.zeros((h, w), dtype=np.uint8)
    for dy in (0, 1, 2):
        for dx in (0, 1, 2):
            out += p[dy:dy + h, dx:dx + w]
    return out


def detect_change(prev_rgb, prev_cls, cur_rgb, cur_cls, threshold=DIFF_THRESHOLD):
    """Return (changed_mask, changed_pct or None if too little usable overlap)."""
    prev = prev_rgb.astype(np.float32)
    cur = cur_rgb.astype(np.float32)
    valid = (prev_cls != UNUSABLE) & (cur_cls != UNUSABLE)
    valid &= ~((prev_cls == WATER) & (cur_cls == WATER))  # open water both times: ignore glint/waves
    valid &= (prev.max(axis=2) > 0) & (cur.max(axis=2) > 0)
    both_seen = (prev_cls != UNUSABLE) & (cur_cls != UNUSABLE)
    if both_seen.mean() < MIN_VALID_FRACTION or valid.sum() == 0:
        return np.zeros(valid.shape, bool), None
    # match overall brightness per band so haze or sun angle doesn't read as change
    for b in range(3):
        pm, cm = prev[..., b][valid].mean(), cur[..., b][valid].mean()
        if cm > 0:
            cur[..., b] *= pm / cm
    dist = np.sqrt(((prev - cur) ** 2).mean(axis=2))
    raw = (dist > threshold) & valid
    changed = raw & (_neighbour_count(raw) >= 5)  # drop speckle
    return changed, float(changed.sum() / max(1, both_seen.sum()) * 100)


def overlay(cur_rgb: np.ndarray, changed: np.ndarray) -> Image.Image:
    grey = (cur_rgb.astype(np.float32).mean(axis=2) * 0.7).astype(np.uint8)
    out = np.stack([grey, grey, grey], axis=2)
    out[changed] = (0.35 * out[changed] + 0.65 * np.array([255, 60, 60])).astype(np.uint8)
    return Image.fromarray(out)


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

def load_index() -> dict:
    if INDEX.exists():
        return json.loads(INDEX.read_text())
    return {"updated": None, "sites": {}}


def save_obs_files(site_id, date, rgb, cls, changed):
    d = DATA / site_id
    d.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgb).save(d / f"{date}.jpg", quality=88)
    Image.fromarray(cls).save(d / f"{date}_cls.png", optimize=True)
    diff_rel = None
    if changed is not None:
        overlay(rgb, changed).save(d / f"{date}_diff.jpg", quality=85)
        diff_rel = f"data/{site_id}/{date}_diff.jpg"
    return f"data/{site_id}/{date}.jpg", diff_rel


def load_obs_arrays(site_id, date):
    d = DATA / site_id
    rgb = np.array(Image.open(d / f"{date}.jpg").convert("RGB"))
    cls = np.array(Image.open(d / f"{date}_cls.png"))
    return rgb, cls


# --------------------------------------------------------------------------
# Imagery access (network)
# --------------------------------------------------------------------------

def search_scenes(site, start: dt.date, end: dt.date, limit=40):
    from pystac_client import Client
    client = Client.open(STAC_URL)
    point = {"type": "Point", "coordinates": [site["lng"], site["lat"]]}
    search = client.search(
        collections=COLLECTIONS, intersects=point,
        datetime=f"{start.isoformat()}/{end.isoformat()}",
        query={"eo:cloud_cover": {"lt": 60}},
        sortby=[{"field": "properties.datetime", "direction": "desc"}],
        max_items=limit,
    )
    best = {}  # one scene per day: the least cloudy
    for item in search.items():
        day = item.datetime.date().isoformat()
        cc = item.properties.get("eo:cloud_cover", 100)
        if day not in best or cc < best[day].properties.get("eo:cloud_cover", 100):
            best[day] = item
    return [best[k] for k in sorted(best)]


def read_crop(href, site, size_px, nearest=False):
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.warp import transform_bounds
    from rasterio.windows import from_bounds
    lat, lng, half = site["lat"], site["lng"], site["half_km"]
    dlat = half / 110.574
    dlng = half / (111.320 * np.cos(np.radians(lat)))
    bounds_ll = (lng - dlng, lat - dlat, lng + dlng, lat + dlat)
    with rasterio.open(href) as src:
        b = transform_bounds("EPSG:4326", src.crs, *bounds_ll, densify_pts=21)
        win = from_bounds(*b, transform=src.transform)
        data = src.read(
            window=win, boundless=True, fill_value=0,
            out_shape=(src.count, size_px, size_px),
            resampling=Resampling.nearest if nearest else Resampling.bilinear,
        )
    return data


def fetch_observation(item, site):
    size = int(round(2 * site["half_km"] * 1000 / 10))
    vis = item.assets.get("visual")
    scl = item.assets.get("scl")
    if vis is None or scl is None:
        return None
    rgb = np.moveaxis(read_crop(vis.href, site, size)[:3], 0, 2).astype(np.uint8)
    cls = classes_from_scl(read_crop(scl.href, site, size, nearest=True)[0])
    return rgb, cls


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def process_site(site, index, today, new_alerts, log):
    rec = index["sites"].setdefault(site["id"], {"observations": []})
    rec.update({k: site[k] for k in ("name", "region", "lat", "lng")})
    obs = rec["observations"]
    seen = {o["scene"] for o in obs}
    usable = [o for o in obs if o.get("usable")]
    first_run = not usable
    start = today - dt.timedelta(days=BACKFILL_DAYS if first_run else LOOKBACK_DAYS)
    if usable:
        start = max(start, dt.date.fromisoformat(usable[-1]["date"]) + dt.timedelta(days=1))
    try:
        seen_days = {o["date"] for o in obs}
        scenes = [s for s in search_scenes(site, start, today)
                  if s.id not in seen and s.datetime.date().isoformat() not in seen_days]
    except Exception as e:  # network/API hiccup: try again next run
        log(f"  search failed: {e}")
        return
    if first_run:
        scenes = scenes[-(BACKFILL_SCENES * 3):]  # enough candidates to find a few clear ones
    log(f"  {len(scenes)} new scene(s) to check")
    kept = 0
    for item in scenes:
        date = item.datetime.date().isoformat()
        try:
            got = fetch_observation(item, site)
        except Exception as e:
            log(f"  {date}: read failed ({e})")
            continue
        if got is None:
            continue
        rgb, cls = got
        bad = unusable_pct(cls, rgb)
        entry = {"date": date, "scene": item.id, "cloud_pct": round(bad, 1), "usable": bad <= MAX_CLOUD_PCT}
        if not entry["usable"]:
            obs.append(entry)
            log(f"  {date}: skipped, {bad:.0f}% cloud or no data")
            continue
        prev = next((o for o in reversed(obs) if o.get("usable")), None)
        changed, pct = None, None
        if prev:
            p_rgb, p_cls = load_obs_arrays(site["id"], prev["date"])
            if p_rgb.shape == rgb.shape:
                changed, pct = detect_change(p_rgb, p_cls, rgb, cls)
        img, diff = save_obs_files(site["id"], date, rgb, cls, changed)
        entry.update(image=img, diff=diff, compared_to=prev["date"] if prev else None,
                     change_pct=None if pct is None else round(pct, 2))
        entry["alert"] = bool(pct is not None and pct >= site["alert_pct"] and not first_run)
        obs.append(entry)
        kept += 1
        log(f"  {date}: kept, change {'n/a' if pct is None else f'{pct:.2f}%'}"
            + ("  <-- ALERT" if entry["alert"] else ""))
        if entry["alert"]:
            new_alerts.append({"site": site["id"], "name": site["name"], **entry})
    obs.sort(key=lambda o: o["date"])


def write_readme(index):
    lines = [
        "# Build Watch monitor",
        "",
        "Automatic change detection on free Sentinel-2 satellite imagery (10 m per pixel) "
        "for publicly reported Chinese construction sites. Runs every two days on GitHub Actions; "
        "big changes open an Issue in this repo.",
        "",
        f"_Last run: {index.get('updated') or 'never'} UTC_",
        "",
        "| Site | Latest clear image | Change vs previous | Alerts |",
        "|---|---|---|---|",
    ]
    for sid, rec in index["sites"].items():
        usable = [o for o in rec["observations"] if o.get("usable")]
        if not usable:
            lines.append(f"| **{rec['name']}**<br>{rec['region']} | no clear image yet | | |")
            continue
        last = usable[-1]
        ch = "first image" if last.get("change_pct") is None else f"{last['change_pct']:.1f}% since {last['compared_to']}"
        n_alerts = sum(1 for o in usable if o.get("alert"))
        diff = f" · [change map]({last['diff']})" if last.get("diff") else ""
        lines.append(
            f"| **{rec['name']}**<br>{rec['region']} | <img src=\"{last['image']}\" width=\"220\"><br>{last['date']}{diff} "
            f"| {ch} | {n_alerts} |"
        )
    lines += ["", "Imagery: Copernicus Sentinel-2 via Element 84 Earth Search. "
              "Change % is the share of the usable area whose colour changed noticeably; "
              "clouds, haze and boats can cause false alarms, so check each alert by eye.", "",
              "**Run it now:** Actions tab → *Monitor sites* → *Run workflow*. "
              "**Add a site:** add a line to `sites.json`. "
              "**Get alerts by email:** Watch this repo → Custom → Issues.", ""]
    (ROOT / "README.md").write_text("\n".join(lines))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", help="only run one site id")
    args = ap.parse_args(argv)
    sites = json.loads((ROOT / "sites.json").read_text())
    if args.site:
        sites = [s for s in sites if s["id"] == args.site]
    os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
    os.environ.setdefault("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif,.TIF,.tiff")
    os.environ.setdefault("AWS_NO_SIGN_REQUEST", "YES")
    index = load_index()
    today = dt.datetime.now(dt.timezone.utc).date()
    new_alerts = []
    for site in sites:
        print(f"{site['name']}:", flush=True)
        process_site(site, index, today, new_alerts, lambda m: print(m, flush=True))
    index["updated"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M")
    DATA.mkdir(exist_ok=True)
    INDEX.write_text(json.dumps(index, indent=1))
    (ROOT / "alerts_new.json").write_text(json.dumps(new_alerts, indent=1))
    write_readme(index)
    print(f"Done. {len(new_alerts)} alert(s).")


if __name__ == "__main__":
    sys.exit(main())
