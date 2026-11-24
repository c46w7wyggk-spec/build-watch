"""Offline tests: no network needed. Run with `pytest -q`."""
import datetime as dt
import json
import types

import numpy as np
import pytest

import monitor


def _scene(rgb_seed=0, size=60):
    r = np.random.default_rng(rgb_seed)
    rgb = (r.integers(90, 110, (size, size, 3))).astype(np.uint8)
    cls = np.full((size, size), monitor.LAND, np.uint8)
    return rgb, cls


def test_no_change_reads_zero():
    rgb, cls = _scene(1)
    rgb2 = np.clip(rgb.astype(int) + 3, 0, 255).astype(np.uint8)  # slight brightness shift only
    _, pct = monitor.detect_change(rgb, cls, rgb2, cls)
    assert pct == 0


def test_new_block_is_detected():
    rgb, cls = _scene(2)
    after = rgb.copy()
    after[20:40, 20:40] = [230, 225, 210]  # new 20x20 pale structure
    changed, pct = monitor.detect_change(rgb, cls, after, cls)
    assert 8 < pct < 14  # 400 / 3600 = 11%
    assert changed[30, 30] and not changed[5, 5]


def test_clouds_and_open_water_are_ignored():
    rgb, cls = _scene(3)
    after = rgb.copy()
    after[0:10] = 255
    cls_after = cls.copy()
    cls_after[0:10] = monitor.UNUSABLE  # cloud band
    water = cls.copy()
    water[40:] = monitor.WATER
    after[40:] = (after[40:] * 0.4).astype(np.uint8)  # waves/glint look different, still water
    _, pct = monitor.detect_change(rgb, water, after, np.where(cls_after == monitor.UNUSABLE, cls_after, water))
    assert pct == 0


def test_too_cloudy_returns_none():
    rgb, cls = _scene(4)
    c2 = cls.copy()
    c2[:40] = monitor.UNUSABLE
    _, pct = monitor.detect_change(rgb, cls, rgb, c2)
    assert pct is None


def test_scl_mapping():
    scl = np.array([[0, 4, 5, 6], [8, 9, 10, 3]])
    assert monitor.classes_from_scl(scl).tolist() == [[0, 1, 1, 2], [0, 0, 0, 0]]


def test_read_crop_from_utm_geotiff(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_origin
    # a fake 10 m UTM tile (zone 49N) around Subi Reef with a bright square at the site
    site = {"lat": 10.923, "lng": 114.084, "half_km": 1.0}
    from rasterio.warp import transform
    xs, ys = transform("EPSG:4326", "EPSG:32649", [site["lng"]], [site["lat"]])
    x, y = xs[0], ys[0]
    tr = from_origin(x - 5000, y + 5000, 10, 10)
    arr = np.zeros((3, 1000, 1000), np.uint8)
    arr[:, 490:510, 490:510] = 255
    p = tmp_path / "tci.tif"
    with rasterio.open(p, "w", driver="GTiff", width=1000, height=1000, count=3, dtype="uint8",
                       crs="EPSG:32649", transform=tr) as dst:
        dst.write(arr)
    out = monitor.read_crop(str(p), site, 200)
    assert out.shape == (3, 200, 200)
    assert out[0, 100, 100] == 255 and out[0, 10, 10] == 0


def test_end_to_end_with_fake_scenes(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "ROOT", tmp_path)
    monkeypatch.setattr(monitor, "DATA", tmp_path / "data")
    monkeypatch.setattr(monitor, "INDEX", tmp_path / "data" / "index.json")
    site = {"id": "t", "name": "Test", "region": "R", "lat": 0, "lng": 0, "half_km": 0.3, "alert_pct": 3}
    base, cls = _scene(5)
    built = base.copy()
    built[10:40, 10:40] = [220, 215, 200]
    cloudy_cls = cls.copy()
    cloudy_cls[:] = monitor.UNUSABLE
    frames = {"a": (base, cls), "b": (base, cloudy_cls), "c": (built, cls)}

    def fake_items(days):
        return [types.SimpleNamespace(id=k, datetime=dt.datetime(2026, 1, d)) for k, d in days]

    calls = {"n": 0}

    def fake_search(s, start, end, limit=40):
        calls["n"] += 1
        return fake_items([("a", 1), ("b", 5)]) if calls["n"] == 1 else fake_items([("a", 1), ("b", 5), ("c", 9)])

    monkeypatch.setattr(monitor, "search_scenes", fake_search)
    monkeypatch.setattr(monitor, "fetch_observation", lambda item, s: frames[item.id])

    index = {"updated": None, "sites": {}}
    alerts = []
    monitor.process_site(site, index, dt.date(2026, 1, 6), alerts, lambda m: None)
    obs = index["sites"]["t"]["observations"]
    assert [o["usable"] for o in obs] == [True, False]
    assert alerts == []

    monitor.process_site(site, index, dt.date(2026, 1, 10), alerts, lambda m: None)
    obs = index["sites"]["t"]["observations"]
    assert [o["scene"] for o in obs] == ["a", "b", "c"]
    last = obs[-1]
    assert last["compared_to"] == "2026-01-01" and last["change_pct"] > 15 and last["alert"]
    assert len(alerts) == 1 and (tmp_path / last["diff"]).exists()

    monitor.write_readme(index)
    assert "Test" in (tmp_path / "README.md").read_text()
