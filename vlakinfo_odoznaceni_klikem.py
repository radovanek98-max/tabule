import argparse
import json
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM_BASE = "https://obehy.cz/api/v1"
import argparse
import json
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM_BASE = "https://obehy.cz/api/v1"
SZ_MU_URL = r"https://mapy.spravazeleznic.cz/serverside/request2.php?module=Layers\MimoradneUdalosti&&action=loadCrawl"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

CACHE_LOCK = threading.Lock()
RAIL_WAYS_CACHE = {}
TRIP_CACHE = {}
DEPARTURES_CACHE = {}

VEHICLES_LOCK = threading.Lock()
CURRENT_VIEWPORT = list((12.05, 48.50, 18.90, 51.10))
LATEST_VEHICLES = {}
LAST_FETCH_TS = 0.0
FETCH_IN_PROGRESS = False

SZ_LOCK = threading.Lock()
LATEST_SZ_INCIDENTS = []


def sjtsk_to_wgs84(y_jtsk, x_jtsk):
    X = abs(float(x_jtsk))
    Y = abs(float(y_jtsk))
    if X < Y:
        X, Y = Y, X

    a = 6377397.15508
    e = 0.081696831215303
    n = 0.97992470462083
    rho0 = 12310230.12797036
    sinUQ = 0.863499969506341
    cosUQ = 0.504348889819882
    sinVQ = 0.420215144586493
    cosVQ = 0.907424504992097
    alpha = 1.000597498371542
    k = 1.003419163966575

    rho = math.sqrt(X * X + Y * Y)
    ad = 2.0 * math.atan(Y / (rho + X))
    t = math.exp(math.log(rho / rho0) / n)
    u = math.pi / 2.0 - 2.0 * math.atan(t)
    v = ad / n

    sinS = math.sin(u)
    cosS = math.cos(u)
    sinD = math.sin(v)
    cosD = math.cos(v)

    sinU = sinUQ * sinS - cosUQ * cosS * cosD
    U = math.asin(max(-1.0, min(1.0, sinU)))
    cosU = math.cos(U)

    sinDV = (sinD * cosS) / cosU
    cosDV = (cosUQ * sinS + sinUQ * cosS * cosD) / cosU
    DV = math.atan2(sinDV, cosDV)
    l_ferro = DV / alpha

    B = U
    for _ in range(5):
        sinB = math.sin(B)
        w = ((1.0 - e * sinB) / (1.0 + e * sinB)) ** (alpha * e / 2.0)
        B = 2.0 * math.atan(k * math.tan(U / 2.0 + math.pi / 4.0) * w) - math.pi / 2.0

    lat_b = B
    lon_b = math.radians(24.833333333333332) - l_ferro

    e2_b = 0.006674372230614
    h = 250.0
    rn_b = a / math.sqrt(1.0 - e2_b * math.sin(lat_b) ** 2)
    xb = (rn_b + h) * math.cos(lat_b) * math.cos(lon_b)
    yb = (rn_b + h) * math.cos(lat_b) * math.sin(lon_b)
    zb = (rn_b * (1.0 - e2_b) + h) * math.sin(lat_b)

    dx, dy, dz = 570.8285, 85.6769, 462.8420
    wx = math.radians(4.9984 / 3600.0)
    wy = math.radians(1.5867 / 3600.0)
    wz = math.radians(5.2611 / 3600.0)
    m = 1.0 + 3.5623e-6

    xw = dx + m * (xb + wz * yb - wy * zb)
    yw = dy + m * (-wz * xb + yb + wx * zb)
    zw = dz + m * (wy * xb - wx * yb + zb)

    a_w = 6378137.0
    e2_w = 0.00669437999014
    p = math.hypot(xw, yw)
    lat_w = math.atan2(zw, p * (1.0 - e2_w))
    for _ in range(5):
        rn_w = a_w / math.sqrt(1.0 - e2_w * math.sin(lat_w) ** 2)
        lat_w = math.atan2(zw + e2_w * rn_w * math.sin(lat_w), p)
        lon_w = math.atan2(yw, xw)

        return round(math.degrees(lat_w) + 0.1190, 6), round(math.degrees(lon_w) + 0.0070, 6)

def snap_sz_line_to_rail(pts):
    if not pts:
        return pts
    lats = [p[0] for p in pts]
    lons = [p[1] for p in pts]
    pad = 0.025
    segments = get_rail_segments_for_bbox(min(lons) - pad, min(lats) - pad, max(lons) + pad, max(lats) + pad)
    if not segments:
        return pts

    snapped_pts = []
    for lat, lon in pts:
        cos_lat = math.cos(math.radians(lat))
        best_dist = 1800.0
        best_lat, best_lon = lat, lon
        for ax, ay, bx, by in segments:
            if abs(ay - lat) > 0.025 and abs(by - lat) > 0.025:
                continue
            if abs(ax - lon) > 0.035 and abs(bx - lon) > 0.035:
                continue
            px, py, _ = project_point_to_segment(lon * cos_lat, lat, ax * cos_lat, ay, bx * cos_lat, by)
            s_lon, s_lat = px / cos_lat, py
            d = haversine_m(lat, lon, s_lat, s_lon)
            if d < best_dist:
                best_dist = d
                best_lat, best_lon = s_lat, s_lon
        snapped_pts.append([round(best_lat, 6), round(best_lon, 6)])
    return snapped_pts


def refresh_sz_incidents():
    global LATEST_SZ_INCIDENTS
    try:
        req = urllib.request.Request(
            SZ_MU_URL,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                "Accept": "*/*",
            },
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = json.loads(resp.read().decode("utf-8"))

        features = (raw.get("mu") or {}).get("features") or []
        parsed = []

        for feat in features:
            props = feat.get("properties") or {}
            geom = feat.get("geometry") or {}
            gtype = geom.get("type")
            coords = geom.get("coordinates") or []
            lines = []

            if gtype == "LineString":
                pts = []
                for c in coords:
                    if isinstance(c, (list, tuple)) and len(c) >= 2:
                        it = iter(c)
                        lat, lon = sjtsk_to_wgs84(next(it), next(it))
                        pts.append([lat, lon])
                if pts:
                    lines.append(pts)
            elif gtype == "MultiLineString":
                for sub in coords:
                    pts = []
                    for c in sub:
                        if isinstance(c, (list, tuple)) and len(c) >= 2:
                            it = iter(c)
                            lat, lon = sjtsk_to_wgs84(next(it), next(it))
                            pts.append([lat, lon])
                    if pts:
                        lines.append(pts)
            elif gtype == "Point":
                if isinstance(coords, (list, tuple)) and len(coords) >= 2:
                    it = iter(coords)
                    lat, lon = sjtsk_to_wgs84(next(it), next(it))
                    lines.append([[lat, lon]])

            if not lines:
                continue

            lines = [snap_sz_line_to_rail(ln) for ln in lines]

            vlaky_str = str(props.get("VLAKY") or "")
            trains = []
            for p in vlaky_str.split("|"):
                if "*" in p:
                    _, tname = p.split("*", 1)
                    if tname.strip():
                        trains.append(tname.strip())

            first_line = next(iter(lines))
            center_pt = first_line[len(first_line) // 2]

            parsed.append({
                "title": props.get("MUC_POPIS") or "Mimoradnost na trati",
                "impact": props.get("MU_DOPAD_CESTUJICI_TXT") or "",
                "impact_code": props.get("MU_DOPAD_CESTUJICI") or 1,
                "start_station": props.get("SR70_START_NAZEV") or "",
                "end_station": props.get("SR70_CIL_NAZEV") or "",
                "end_time": props.get("MU_PREDPOKLAD_KONEC") or "",
                "trains": trains,
                "lines": lines,
                "center": center_pt,
            })

        with SZ_LOCK:
            LATEST_SZ_INCIDENTS = parsed
        print(f"SZ mimoradnosti nacteny: {len(parsed)}")
    except Exception as exc:
        print("Chyba SZ:", exc)





def fetch_json(url, timeout=10):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "VlakInfoCZ/4.3 (LiveRailMap)",
            "Accept": "application/json",
            "Cache-Control": "no-cache",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))
    


def fetch_json(url, timeout=10):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "VlakInfoCZ/4.3 (LiveRailMap)",
            "Accept": "application/json",
            "Cache-Control": "no-cache",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def split_bbox(min_lon, min_lat, max_lon, max_lat, max_step=1.8):
    lon_span = max(0.05, max_lon - min_lon)
    lat_span = max(0.05, max_lat - min_lat)
    cols = max(1, min(5, math.ceil(lon_span / max_step)))
    rows = max(1, min(4, math.ceil(lat_span / max_step)))
    lon_step = lon_span / cols
    lat_step = lat_span / rows

    tiles = []
    for r in range(rows):
        for c in range(cols):
            t_min_lon = round(min_lon + c * lon_step, 4)
            t_max_lon = round(min_lon + (c + 1) * lon_step, 4)
            t_min_lat = round(min_lat + r * lat_step, 4)
            t_max_lat = round(min_lat + (r + 1) * lat_step, 4)
            tiles.append((t_min_lon, t_min_lat, t_max_lon, t_max_lat))
    return tiles


def fetch_tile_with_retry(tile):
    t_min_lon, t_min_lat, t_max_lon, t_max_lat = tile
    bbox_str = f"{t_min_lon},{t_min_lat},{t_max_lon},{t_max_lat}"
    url = f"{UPSTREAM_BASE}/vehicles?bbox={urllib.parse.quote(bbox_str)}"
    for attempt in range(2):
        try:
            data = fetch_json(url, timeout=8)
            return data.get("vehicles", [])
        except Exception:
            if attempt == 0:
                time.sleep(0.25)
    return []



def project_point_to_segment(px, py, ax, ay, bx, by):
    dx = bx - ax
    dy = by - ay
    len_sq = dx * dx + dy * dy
    if len_sq == 0:
        return ax, ay, 0.0
    t = ((px - ax) * dx + (py - ay) * dy) / len_sq
    t = max(0.0, min(1.0, t))
    proj_x = ax + t * dx
    proj_y = ay + t * dy
    seg_bearing = (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0
    return proj_x, proj_y, seg_bearing


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def get_rail_segments_for_bbox(min_lon, min_lat, max_lon, max_lat):
    key = (round(min_lon, 1), round(min_lat, 1), round(max_lon, 1), round(max_lat, 1))
    with CACHE_LOCK:
        if key in RAIL_WAYS_CACHE:
            return RAIL_WAYS_CACHE[key]

    query = f"""
    [out:json][timeout:12];
    (
      way["railway"~"^(rail|light_rail)$"]({min_lat},{min_lon},{max_lat},{max_lon});
    );
    out geom;
    """
    try:
        data = urllib.parse.urlencode({"data": query}).encode("utf-8")
        req = urllib.request.Request(
            OVERPASS_URL,
            data=data,
            headers={"User-Agent": "VlakInfoCZ/4.3"},
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            parsed = json.loads(resp.read().decode("utf-8"))

        segments = []
        for el in parsed.get("elements", []):
            geom = el.get("geometry", [])
            for i in range(len(geom) - 1):
                segments.append(
                    (
                        geom[i]["lon"],
                        geom[i]["lat"],
                        geom[i + 1]["lon"],
                        geom[i + 1]["lat"],
                    )
                )
        with CACHE_LOCK:
            if len(RAIL_WAYS_CACHE) > 80:
                RAIL_WAYS_CACHE.clear()
            RAIL_WAYS_CACHE[key] = segments
        return segments
    except Exception:
        return []


def snap_train_to_rail(train, segments):
    lat = train.get("latitude")
    lon = train.get("longitude")
    if lat is None or lon is None or not segments:
        return train

    cos_lat = math.cos(math.radians(lat))
    best_dist = 420.0
    best_lat = lat
    best_lon = lon
    best_bearing = train.get("bearing")
    orig_bearing = train.get("bearing")

    for ax, ay, bx, by in segments:
        if abs(ay - lat) > 0.02 and abs(by - lat) > 0.02:
            continue
        if abs(ax - lon) > 0.03 and abs(bx - lon) > 0.03:
            continue

        px, py, seg_b = project_point_to_segment(
            lon * cos_lat, lat, ax * cos_lat, ay, bx * cos_lat, by
        )
        snapped_lon = px / cos_lat
        snapped_lat = py
        dist = haversine_m(lat, lon, snapped_lat, snapped_lon)

        if orig_bearing is not None:
            diff1 = abs((seg_b - orig_bearing + 180) % 360 - 180)
            rev_b = (seg_b + 180) % 360
            diff2 = abs((rev_b - orig_bearing + 180) % 360 - 180)
            angle_penalty = min(diff1, diff2) * 0.6
            aligned_b = seg_b if diff1 <= diff2 else rev_b
        else:
            angle_penalty = 0.0
            aligned_b = seg_b

        score = dist + angle_penalty
        if dist <= 420.0 and score < best_dist:
            best_dist = score
            best_lat = snapped_lat
            best_lon = snapped_lon
            best_bearing = aligned_b

    train["latitude"] = best_lat
    train["longitude"] = best_lon
    if best_bearing is not None:
        train["bearing"] = round(best_bearing, 1)
    return train


def refresh_vehicles_once():
    global LAST_FETCH_TS, FETCH_IN_PROGRESS
    with VEHICLES_LOCK:
        if FETCH_IN_PROGRESS:
            return
        FETCH_IN_PROGRESS = True
        min_lon, min_lat, max_lon, max_lat = CURRENT_VIEWPORT

    try:
        tiles = split_bbox(min_lon, min_lat, max_lon, max_lat, max_step=1.8)
        collected = {}

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(fetch_tile_with_retry, t): t for t in tiles}
            for fut in as_completed(futures):
                for v in fut.result():
                    line = v.get("line") or {}
                    if line.get("mode") == "rail":
                        vid = v.get("vehicle_id")
                        if vid:
                            collected[vid] = v

        span = max(max_lon - min_lon, max_lat - min_lat)
        segments = []
        if span <= 3.2:
            segments = get_rail_segments_for_bbox(min_lon, min_lat, max_lon, max_lat)

        now = time.time()
        with VEHICLES_LOCK:
            for vid, train in collected.items():
                if segments:
                    train = snap_train_to_rail(train, segments)
                LATEST_VEHICLES[vid] = {"data": train, "seen": now}

            expired = [k for k, item in LATEST_VEHICLES.items() if now - item["seen"] > 60.0]
            for k in expired:
                del LATEST_VEHICLES[k]

            LAST_FETCH_TS = now
            print(f"Stazeno vlaku: {len(LATEST_VEHICLES)}")
    finally:
        with VEHICLES_LOCK:
            FETCH_IN_PROGRESS = False



def background_poller():
    counter = 0
    while True:
        refresh_vehicles_once()
        counter += 1
        if counter >= 12:
            sjtsk_to_wgs84
            refresh_sz_incidents()
            counter = 0
        time.sleep(2.5)



HTML_PAGE = r"""<!DOCTYPE html>
<html lang="cs">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>VlakInfo LIVE – Mapa zpoždění vlaků v ČR</title>
    <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🚆</text></svg>">
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <style>
        :root {
            --bg-header: #0f172a;
            --bg-panel: #ffffff;
            --border-color: #e2e8f0;
            --text-main: #0f172a;
            --text-muted: #64748b;
            --accent: #2563eb;
        }
        * { box-sizing: border-box; }
        body {
            margin: 0; padding: 0;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, sans-serif;
            color: var(--text-main);
            overflow: hidden;
            background: #f1f5f9;
        }

        .topbar {
            height: 60px;
            background: linear-gradient(90deg, #0f172a 0%, #1e293b 100%);
            color: white;
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 0 16px;
            gap: 10px;
            box-shadow: 0 2px 10px rgba(15, 23, 42, 0.28);
            z-index: 1200;
            position: relative;
        }
        .topbar-left {
            display: flex;
            align-items: center;
            gap: 8px;
            min-width: 0;
        }
        .brand {
            display: flex; align-items: center; gap: 7px;
            font-weight: 700; font-size: 16px; letter-spacing: -0.01em;
            margin-right: 4px; white-space: nowrap;
        }
        .live-pill {
            display: inline-flex; align-items: center; gap: 5px;
            background: rgba(22, 163, 74, 0.2); border: 1px solid rgba(34, 197, 94, 0.45);
            color: #4ade80; font-size: 11px; font-weight: 700;
            padding: 2px 8px; border-radius: 999px;
        }
        .live-dot {
            width: 7px; height: 7px; border-radius: 50%; background: #22c55e;
            animation: pulse 1.6s infinite;
        }
        @keyframes pulse {
            0% { opacity: 1; transform: scale(1); }
            50% { opacity: 0.4; transform: scale(1.25); }
            100% { opacity: 1; transform: scale(1); }
        }

        .filter-control {
            background: rgba(255, 255, 255, 0.1);
            border: 1px solid rgba(255, 255, 255, 0.16);
            color: white;
            border-radius: 8px;
            padding: 6px 9px;
            font-size: 12.5px;
            outline: none;
            transition: border-color 0.15s, background 0.15s;
        }
        .filter-control:hover, .filter-control:focus {
            background: rgba(255, 255, 255, 0.16);
            border-color: rgba(255, 255, 255, 0.35);
        }
        .filter-control option { background: #1e293b; color: white; }
        .search-box { width: 155px; }

        /* Velký centrovaný čas a datum */
        .center-clock-box {
            display: flex;
            align-items: baseline;
            gap: 12px;
            background: rgba(15, 23, 42, 0.65);
            border: 1px solid rgba(96, 165, 250, 0.4);
            padding: 6px 18px;
            border-radius: 10px;
            box-shadow: inset 0 1px 0 rgba(255,255,255,0.08);
            white-space: nowrap;
            flex-shrink: 0;
        }
        .clock-date {
            font-size: 14px;
            font-weight: 600;
            color: #93c5fd;
        }
        .clock-time {
            font-size: 21px;
            font-weight: 800;
            color: #ffffff;
            font-variant-numeric: tabular-nums;
            letter-spacing: 0.03em;
        }

        .stats-bar {
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 12px;
            color: #cbd5e1;
        }
        .stat-chip {
            background: rgba(255, 255, 255, 0.08);
            padding: 5px 9px;
            border-radius: 7px;
            white-space: nowrap;
        }
        .stat-chip strong { color: white; }
        .max-delay-btn {
            background: #dc2626;
            color: white;
            border: none;
            border-radius: 7px;
            padding: 5px 9px;
            font-size: 12px;
            font-weight: 600;
            cursor: pointer;
            transition: background 0.15s;
            white-space: nowrap;
        }
        .max-delay-btn:hover { background: #b91c1c; }

        .workspace {
            display: flex;
            height: calc(100vh - 60px);
            width: 100vw;
        }
        #map {
            flex: 1;
            height: 100%;
            z-index: 1;
        }

        .sidebar {
            width: 390px;
            background: var(--bg-panel);
            border-left: 1px solid var(--border-color);
            display: flex;
            flex-direction: column;
            box-shadow: -4px 0 16px rgba(15, 23, 42, 0.06);
            z-index: 1000;
        }
        .tabs {
            display: flex;
            border-bottom: 1px solid var(--border-color);
            background: #f8fafc;
        }
        .tab-btn {
            flex: 1;
            padding: 11px 8px;
            border: none;
            background: transparent;
            font-size: 13px;
            font-weight: 600;
            color: var(--text-muted);
            cursor: pointer;
            border-bottom: 2px solid transparent;
            transition: all 0.15s;
        }
        .tab-btn:hover { color: var(--text-main); }
        .tab-btn.active {
            color: var(--accent);
            border-bottom-color: var(--accent);
            background: white;
        }
        .tab-content {
            flex: 1;
            overflow-y: auto;
            padding: 0;
        }

        .train-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 12.5px;
        }
        .train-table th {
            position: sticky; top: 0;
            background: #f8fafc;
            color: var(--text-muted);
            font-weight: 600;
            text-align: left;
            padding: 9px 12px;
            border-bottom: 1px solid var(--border-color);
            cursor: pointer;
            user-select: none;
            z-index: 2;
        }
        .train-table td {
            padding: 8px 12px;
            border-bottom: 1px solid #f1f5f9;
            vertical-align: middle;
        }
        .train-row { cursor: pointer; transition: background 0.1s; }
        .train-row:hover { background: #f8fafc; }
        .train-row.selected { background: #eff6ff; }
        .train-name { font-weight: 700; color: #0f172a; }
        .carrier-sub { font-size: 11px; color: var(--text-muted); }

        .delay-pill {
            display: inline-block;
            padding: 3px 8px;
            border-radius: 6px;
            font-weight: 700;
            font-size: 11.5px;
            color: white;
            text-align: center;
            min-width: 58px;
        }

        .detail-box { padding: 14px; }
        .detail-header {
            background: #f8fafc;
            border: 1px solid var(--border-color);
            border-radius: 10px;
            padding: 12px;
            margin-bottom: 12px;
        }
        .detail-title { font-size: 16px; font-weight: 700; margin: 0 0 4px 0; }
        .detail-meta { font-size: 12.5px; color: var(--text-muted); line-height: 1.5; }
        .detail-actions {
            display: flex;
            gap: 8px;
            margin-top: 9px;
            flex-wrap: wrap;
        }
        .follow-btn {
            padding: 5px 10px;
            border-radius: 6px;
            border: 1px solid #cbd5e1;
            background: white;
            font-size: 12px;
            font-weight: 600;
            cursor: pointer;
        }
        .follow-btn.active {
            background: #2563eb;
            color: white;
            border-color: #2563eb;
        }
        .deselect-btn {
            padding: 5px 10px;
            border-radius: 6px;
            border: 1px solid #cbd5e1;
            background: #f8fafc;
            color: #475569;
            font-size: 12px;
            font-weight: 600;
            cursor: pointer;
        }
        .deselect-btn:hover { background: #e2e8f0; color: #0f172a; }

        .alert-card {
            background: #fef2f2;
            border: 1px solid #fecaca;
            border-left: 4px solid #dc2626;
            border-radius: 8px;
            padding: 10px 12px;
            margin-bottom: 12px;
            font-size: 12.5px;
            color: #991b1b;
        }
        .turnaround-card {
            background: #eff6ff;
            border: 1px solid #bfdbfe;
            border-left: 4px solid #2563eb;
            border-radius: 8px;
            padding: 10px 12px;
            margin-bottom: 12px;
            font-size: 12.5px;
            color: #1e40af;
        }

        .timetable-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 12.5px;
            border: 1px solid var(--border-color);
            border-radius: 8px;
            overflow: hidden;
        }
        .timetable-table th {
            background: #f8fafc;
            padding: 8px 10px;
            text-align: left;
            color: var(--text-muted);
            font-weight: 600;
            border-bottom: 1px solid var(--border-color);
        }
        .timetable-table td {
            padding: 7px 10px;
            border-bottom: 1px solid #f1f5f9;
        }
        .stop-link {
            color: #0f172a;
            font-weight: 600;
            cursor: pointer;
            text-decoration: none;
        }
        .stop-link:hover { color: var(--accent); text-decoration: underline; }

        .train-dot-compact {
            width: 14px; height: 14px;
            border-radius: 50%;
            border: 2px solid white;
            box-shadow: 0 1px 4px rgba(0,0,0,0.45);
            position: relative;
        }
        .train-dot-compact.selected-ring,
        .train-badge-full.selected-ring {
            box-shadow: 0 0 0 3px #2563eb, 0 3px 10px rgba(15, 23, 42, 0.55);
        }
        .train-badge-full {
            width: 32px; height: 32px;
            border-radius: 50%;
            border: 2.5px solid white;
            color: white;
            font-weight: 800;
            font-size: 11px;
            display: flex;
            align-items: center;
            justify-content: center;
            box-shadow: 0 3px 8px rgba(15, 23, 42, 0.4);
            position: relative;
        }
        .train-badge-full.layover { border-style: dashed; }
        .train-arrow {
            position: absolute;
            top: -8px;
            left: 50%;
            width: 0; height: 0;
            border-left: 5px solid transparent;
            border-right: 5px solid transparent;
            border-bottom: 8px solid #0f172a;
            transform-origin: 50% 24px;
            margin-left: -5px;
        }
        .train-label-tag {
            position: absolute;
            top: 34px;
            left: 50%;
            transform: translateX(-50%);
            background: rgba(15, 23, 42, 0.85);
            color: white;
            font-size: 10px;
            font-weight: 600;
            padding: 1px 5px;
            border-radius: 4px;
            white-space: nowrap;
            pointer-events: none;
        }

        .legend-card {
            position: absolute;
            bottom: 22px;
            left: 14px;
            z-index: 1000;
            background: rgba(255, 255, 255, 0.95);
            backdrop-filter: blur(6px);
            padding: 10px 14px;
            border-radius: 10px;
            box-shadow: 0 4px 14px rgba(15, 23, 42, 0.15);
            border: 1px solid var(--border-color);
            font-size: 12px;
            line-height: 1.65;
        }
        .legend-row { display: flex; align-items: center; gap: 8px; }
        .legend-dot { width: 11px; height: 11px; border-radius: 50%; display: inline-block; }

        .empty-state {
            padding: 32px 20px;
            text-align: center;
            color: var(--text-muted);
            font-size: 13px;
            line-height: 1.5;
        }
        /* Responzivní úpravy pro tablety a mobily */
@media (max-width: 1150px) {
    .center-clock-box { display: none; }
}

@media (max-width: 900px) {
    .topbar { height: auto; flex-wrap: wrap; padding: 8px 10px; gap: 6px; }
    .topbar-left { flex-wrap: wrap; width: 100%; }
    .workspace { flex-direction: column; height: calc(100dvh - 115px); }
    #map { flex: none; height: 52%; width: 100%; }
    .sidebar {
        width: 100%;
        height: 48%;
        border-left: none;
        border-top: 2px solid var(--border-color);
    }
    .legend-card { bottom: 10px; left: 10px; padding: 6px 9px; font-size: 10.5px; }
}

@media (max-width: 600px) {
    .filter-control { flex: 1 1 auto; font-size: 12px; padding: 5px 6px; }
    .search-box { width: 100%; flex-basis: 100%; }
    .stats-bar { width: 100%; justify-content: space-between; font-size: 11px; }
    .legend-card { display: none; }
}

    </style>
</head>
<body>

<div class="topbar">
    <div class="topbar-left">
        <div class="brand">
            <span>🚆 VlakInfo</span>
            <span class="live-pill"><span class="live-dot"></span> LIVE</span>
        </div>

        <select id="carrier-filter" class="filter-control">
            <option value="ALL">Všichni dopravci</option>
            <option value="CD">ČD</option>
            <option value="RJ">RegioJet</option>
            <option value="LE">Leo Express</option>
            <option value="ARR">Arriva</option>
            <option value="OTHER">Ostatní</option>
        </select>

        <select id="category-filter" class="filter-control">
            <option value="ALL">Všechny vlaky</option>
            <option value="LONG">Dálkové</option>
            <option value="REG">Regionální</option>
        </select>

        <select id="delay-filter" class="filter-control">
            <option value="0">Všechna zpoždění</option>
            <option value="5">5+ min</option>
            <option value="15">15+ min</option>
        </select>

        <input type="text" id="search-input" class="filter-control search-box" placeholder="Číslo vlaku / cíl...">
    </div>

    <div class="center-clock-box" title="Aktuální datum a čas">
        <span id="live-date" class="clock-date">--. --. ----</span>
        <span id="live-time" class="clock-time">--:--:--</span>
    </div>

    <div class="stats-bar">
        <div class="stat-chip">Vlaky: <strong id="stat-count">0</strong></div>
        <div class="stat-chip">Průměr: <strong id="stat-avg">0m</strong></div>
        <div class="stat-chip">Včas: <strong id="stat-ontime">0%</strong></div>
        <button id="max-delay-btn" class="max-delay-btn" style="display:none;">Max: -</button>
    </div>
</div>

<div class="workspace">
    <div id="map"></div>

    <div class="sidebar">
        <div class="tabs">
            <button class="tab-btn active" data-tab="list">Seznam vlaků</button>
            <button class="tab-btn" data-tab="detail">Detail spoje</button>
            <button class="tab-btn" data-tab="station">Odjezdy stanice</button>
        </div>

        <div id="tab-list" class="tab-content">
            <table class="train-table">
                <thead>
                    <tr>
                        <th data-sort="name">Vlak</th>
                        <th data-sort="headsign">Cílová stanice</th>
                        <th data-sort="delay" style="text-align:right;">Zpoždění</th>
                    </tr>
                </thead>
                <tbody id="train-tbody"></tbody>
            </table>
        </div>

        <div id="tab-detail" class="tab-content" style="display:none;">
            <div class="empty-state">Klikněte na libovolný vlak v mapě nebo v seznamu pro zobrazení jízdního řádu a mimořádností.</div>
        </div>

        <div id="tab-station" class="tab-content" style="display:none;">
            <div class="empty-state">Klikněte na kroužek stanice v mapě nebo na název stanice v jízdním řádu pro otevření odjezdové tabule.</div>
        </div>
    </div>
</div>

<div class="legend-card">
    <div style="font-weight:700; margin-bottom:4px;">Zpoždění (Vonatinfó)</div>
    <div class="legend-row"><span class="legend-dot" style="background:#16a34a"></span> 0 – 4 min (včas)</div>
    <div class="legend-row"><span class="legend-dot" style="background:#eab308"></span> 5 – 14 min</div>
    <div class="legend-row"><span class="legend-dot" style="background:#f97316"></span> 15 – 59 min</div>
    <div class="legend-row"><span class="legend-dot" style="background:#dc2626"></span> 60+ min</div>
    <div class="legend-row"><span class="legend-dot" style="background:#64748b"></span> Neznámé / obrat</div>
</div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
    const DAYS_CS = ["neděle", "pondělí", "úterý", "středa", "čtvrtek", "pátek", "sobota"];
function updateClock() {
    const now = new Date();
    const pad = n => String(n).padStart(2, '0');
    document.getElementById('live-date').textContent = `${DAYS_CS[now.getDay()]} ${pad(now.getDate())}. ${pad(now.getMonth() + 1)}. ${now.getFullYear()}`;
    document.getElementById('live-time').textContent = `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
}

    updateClock();
    setInterval(updateClock, 1000);

    const map = L.map('map', { zoomControl: true }).setView([49.80, 15.45], 8);
    window.addEventListener('resize', () => {
    setTimeout(() => map.invalidateSize(), 150);
});


    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        attribution: '&copy; OpenStreetMap | Data: obehy.cz',
        maxZoom: 18
    }).addTo(map);

    const markersLayer = L.layerGroup().addTo(map);
    const routeLayer = L.layerGroup().addTo(map);

    const markerRegistry = {};
    const clientVehiclesCache = {};
    let selectedVehicleId = null;
    let followSelectedTrain = false;
    let sortField = 'delay';
    let sortDesc = true;
    let maxDelayTrain = null;
    let detailRefreshInFlight = false;

    function clearSelectedTrain() {
        if (!selectedVehicleId && !followSelectedTrain) return;
        selectedVehicleId = null;
        followSelectedTrain = false;
        routeLayer.clearLayers();
        document.getElementById('tab-detail').innerHTML =
            '<div class="empty-state">Klikněte na libovolný vlak v mapě nebo v seznamu pro zobrazení jízdního řádu a mimořádností.</div>';
        switchTab('list');
        renderAll();
    }

    // Volné kliknutí přímo do plochy mapy odoznačí vybraný vlak
    map.on('click', () => {
        clearSelectedTrain();
    });

    function getDelayColor(delayS) {
        if (delayS === null || delayS === undefined) return '#64748b';
        const min = delayS / 60;
        if (min < 5) return '#16a34a';
        if (min < 15) return '#eab308';
        if (min < 60) return '#f97316';
        return '#dc2626';
    }

    function getPassedRowStyle(delayMin) {
        if (delayMin < 5) return 'background:#dcfce7; color:#14532d;';
        if (delayMin < 15) return 'background:#fef9c3; color:#713f12;';
        if (delayMin < 60) return 'background:#ffedd5; color:#7c2d12;';
        return 'background:#fee2e2; color:#7f1d1d;';
    }

    function formatDelayText(delayS) {
        if (delayS === null || delayS === undefined) return '—';
        const min = Math.round(delayS / 60);
        if (min === 0) return '0 min';
        return min > 0 ? `+${min} min` : `${min} min`;
    }

    function getCategoryBadge(name) {
        if (!name) return 'V';
        const part = name.split(' ')[0];
        return part.length <= 3 ? part : part.substring(0, 2);
    }

    function detectCarrier(train) {
        const name = (train.trip_short_name || '').toUpperCase();
        if (name.startsWith('RJ ')) return 'RJ';
        if (name.startsWith('LE ')) return 'LE';
        if (name.startsWith('ARR ')) return 'ARR';
        if (/^(EC|IC|EX|R|SP|OS|SC|EN|NJ)\s/.test(name)) return 'CD';
        return 'OTHER';
    }

    function isLongDistance(train) {
        const name = (train.trip_short_name || '').toUpperCase();
        return /^(EC|IC|EX|R|RJ|LE|SC|EN|NJ)\s/.test(name);
    }

    function switchTab(tabName) {
        document.querySelectorAll('.tab-btn').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.tab === tabName);
        });
        document.querySelectorAll('.tab-content').forEach(el => {
            el.style.display = el.id === `tab-${tabName}` ? 'block' : 'none';
        });
    }

    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.addEventListener('click', () => switchTab(btn.dataset.tab));
    });

    function buildTrainIcon(train, zoom) {
        const color = getDelayColor(train.delay_s);
        const isLayover = train.state === 'layover' || train.state === 'positioning';
        const isSelected = train.vehicle_id === selectedVehicleId;

        if (zoom <= 9) {
            return L.divIcon({
                className: '',
                html: `<div class="train-dot-compact ${isSelected ? 'selected-ring' : ''}" style="background:${color};"></div>`,
                iconSize: [14, 14],
                iconAnchor: [7, 7]
            });
        }

        const cat = getCategoryBadge(train.trip_short_name);
        const label = train.trip_short_name || '';
        const arrowHtml = (train.bearing !== null && train.bearing !== undefined && !isLayover)
            ? `<div class="train-arrow" style="transform: rotate(${train.bearing}deg);"></div>`
            : '';
        const tagHtml = zoom >= 11 && label ? `<div class="train-label-tag">${label}</div>` : '';

        return L.divIcon({
            className: '',
            html: `
                <div class="train-badge-full ${isLayover ? 'layover' : ''} ${isSelected ? 'selected-ring' : ''}" style="background:${color};">
                    ${arrowHtml}
                    <span>${cat}</span>
                    ${tagHtml}
                </div>
            `,
            iconSize: [32, 32],
            iconAnchor: [16, 16]
        });
    }

    function haversineMeters(lat1, lon1, lat2, lon2) {
        const R = 6371000;
        const toRad = x => x * Math.PI / 180;
        const dLat = toRad(lat2 - lat1);
        const dLon = toRad(lon2 - lon1);
        const a = Math.sin(dLat / 2) ** 2 +
                  Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
        return 2 * R * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
    }

    function renderAll() {
        const carrierFilter = document.getElementById('carrier-filter').value;
        const catFilter = document.getElementById('category-filter').value;
        const minDelayFilter = parseInt(document.getElementById('delay-filter').value, 10);
        const searchQuery = document.getElementById('search-input').value.trim().toLowerCase();
        const zoom = map.getZoom();

        const visibleTrains = [];
        const activeIds = new Set();

        Object.values(clientVehiclesCache).forEach(entry => {
            const train = entry.data;
            if (!train.latitude || !train.longitude) return;

            if (carrierFilter !== 'ALL' && detectCarrier(train) !== carrierFilter) return;
            if (catFilter === 'LONG' && !isLongDistance(train)) return;
            if (catFilter === 'REG' && isLongDistance(train)) return;

            const delayMin = train.delay_s !== null && train.delay_s !== undefined ? Math.round(train.delay_s / 60) : 0;
            if (minDelayFilter > 0 && delayMin < minDelayFilter) return;

            const nameStr = (train.trip_short_name || '').toLowerCase();
            const headStr = (train.headsign || '').toLowerCase();
            if (searchQuery && !nameStr.includes(searchQuery) && !headStr.includes(searchQuery)) return;

            visibleTrains.push(train);
            activeIds.add(train.vehicle_id);

            const icon = buildTrainIcon(train, zoom);
            if (markerRegistry[train.vehicle_id]) {
                const m = markerRegistry[train.vehicle_id];
                m.setLatLng([train.latitude, train.longitude]);
                m.setIcon(icon);
            } else {
                const m = L.marker([train.latitude, train.longitude], { icon });
                m.on('click', (e) => {
                    L.DomEvent.stopPropagation(e);
                    const latest = clientVehiclesCache[train.vehicle_id]?.data || train;
                    openTrainDetail(latest, false);
                });
                m.addTo(markersLayer);
                markerRegistry[train.vehicle_id] = m;
            }
        });

        Object.keys(markerRegistry).forEach(vid => {
            if (!activeIds.has(vid)) {
                markersLayer.removeLayer(markerRegistry[vid]);
                delete markerRegistry[vid];
            }
        });

        if (selectedVehicleId && clientVehiclesCache[selectedVehicleId]) {
            const activeTrain = clientVehiclesCache[selectedVehicleId].data;
            if (followSelectedTrain) {
                map.panTo([activeTrain.latitude, activeTrain.longitude], { animate: true });
            }
            const detailTabVisible = document.getElementById('tab-detail').style.display !== 'none';
            if (detailTabVisible) {
                openTrainDetail(activeTrain, true);
            }
        }

        updateStats(visibleTrains);
        renderSidebarTable(visibleTrains);
    }

    function updateStats(trains) {
        document.getElementById('stat-count').textContent = trains.length;
        let sumDelay = 0;
        let knownCount = 0;
        let onTimeCount = 0;
        let worst = null;

        trains.forEach(t => {
            if (t.delay_s !== null && t.delay_s !== undefined) {
                const m = Math.max(0, t.delay_s / 60);
                sumDelay += m;
                knownCount++;
                if (m < 5) onTimeCount++;
                if (!worst || t.delay_s > worst.delay_s) worst = t;
            }
        });

        const avg = knownCount ? Math.round(sumDelay / knownCount) : 0;
        const onTimePct = knownCount ? Math.round((onTimeCount / knownCount) * 100) : 0;

        document.getElementById('stat-avg').textContent = `${avg}m`;
        document.getElementById('stat-ontime').textContent = `${onTimePct}%`;

        const maxBtn = document.getElementById('max-delay-btn');
        if (worst && worst.delay_s >= 300) {
            maxDelayTrain = worst;
            maxBtn.style.display = 'inline-block';
            maxBtn.textContent = `Max: ${worst.trip_short_name} (+${Math.round(worst.delay_s / 60)}m)`;
        } else {
            maxBtn.style.display = 'none';
        }
    }

    document.getElementById('max-delay-btn').addEventListener('click', () => {
        if (!maxDelayTrain) return;
        map.setView([maxDelayTrain.latitude, maxDelayTrain.longitude], 12);
        openTrainDetail(maxDelayTrain, false);
    });

    function renderSidebarTable(trains) {
        const sorted = [...trains].sort((a, b) => {
            let cmp = 0;
            if (sortField === 'delay') {
                cmp = (a.delay_s ?? -9999) - (b.delay_s ?? -9999);
            } else if (sortField === 'name') {
                cmp = (a.trip_short_name || '').localeCompare(b.trip_short_name || '', 'cs');
            } else {
                cmp = (a.headsign || '').localeCompare(b.headsign || '', 'cs');
            }
            return sortDesc ? -cmp : cmp;
        });

        const tbody = document.getElementById('train-tbody');
        tbody.innerHTML = sorted.slice(0, 180).map(t => {
            const color = getDelayColor(t.delay_s);
            const selected = t.vehicle_id === selectedVehicleId ? 'selected' : '';
            const stateTag = (t.state === 'layover' || t.state === 'positioning') ? ' · OBRAT' : '';
            return `
                <tr class="train-row ${selected}" data-vid="${t.vehicle_id}">
                    <td>
                        <div class="train-name">${t.trip_short_name || 'Vlak'}</div>
                        <div class="carrier-sub">${t.line?.short_name || ''}${stateTag}</div>
                    </td>
                    <td>${t.headsign || '—'}</td>
                    <td style="text-align:right;">
                        <span class="delay-pill" style="background:${color};">${formatDelayText(t.delay_s)}</span>
                    </td>
                </tr>
            `;
        }).join('');

        tbody.querySelectorAll('.train-row').forEach(row => {
            row.addEventListener('click', () => {
                const t = clientVehiclesCache[row.dataset.vid]?.data;
                if (t) {
                    map.setView([t.latitude, t.longitude], Math.max(map.getZoom(), 11));
                    openTrainDetail(t, false);
                }
            });
        });
    }

    document.querySelectorAll('.train-table th').forEach(th => {
        th.addEventListener('click', () => {
            const field = th.dataset.sort;
            if (sortField === field) {
                sortDesc = !sortDesc;
            } else {
                sortField = field;
                sortDesc = field === 'delay';
            }
            renderAll();
        });
    });

    async function openTrainDetail(train, silent = false) {
        if (silent && detailRefreshInFlight) return;
        detailRefreshInFlight = true;

        const prevSelected = selectedVehicleId;
        selectedVehicleId = train.vehicle_id;
        if (prevSelected !== selectedVehicleId) {
            renderAll();
        }

        const container = document.getElementById('tab-detail');
        const prevScrollTop = container.scrollTop;

        if (!silent) {
            switchTab('detail');
            routeLayer.clearLayers();
            container.innerHTML = `<div class="empty-state">Načítám jízdní řád spoje <strong>${train.trip_short_name || ''}</strong>...</div>`;
        }

        try {
            const encodedTrip = encodeURIComponent(train.trip_id);
            const res = await fetch(`/api/trip?trip_id=${encodedTrip}&date=${encodeURIComponent(train.service_date)}`);
            const trip = await res.json();

            if (selectedVehicleId !== train.vehicle_id) return;
            
                        const szMatched = findSzIncidentsForTrain(trip.trip_short_name || train.trip_short_name);
            const szAlertsHtml = szMatched.map(inc => {
                const isStopped = inc.impact_code === 2 || (inc.impact || '').toLowerCase().includes('zastaven');
                return `
                    <div class="alert-card" style="${isStopped ? '' : 'background:#fffbeb;border-color:#fde68a;border-left-color:#f59e0b;color:#92400e;'}">
                        <strong>⚠️ Mimořádnost Správy železnic (${inc.impact}):</strong><br>
                        ${inc.title} (${inc.start_station}${inc.end_station ? ' – ' + inc.end_station : ''})<br>
                        <span style="font-size:11.5px;">Předpoklad ukončení: <strong>${inc.end_time || 'neuvedeno'}</strong></span>
                    </div>
                `;
            }).join('');


            const alertsHtml = (trip.alerts || []).map(a => `
                <div class="alert-card">
                    <strong>⚠️ Mimořádnost v provozu:</strong><br>
                    ${a.title || a.text || a.description || 'Omezení na trati'}
                </div>
            `).join('');

            let turnaroundHtml = '';
            if (trip.run_parts && trip.run_parts.length > 1) {
                turnaroundHtml = `<div class="turnaround-card">🔄 Spoj pokračuje jako navazující část trasy (${trip.run_parts.length} úseky).</div>`;
            }

            const calls = trip.calls || [];
            let closestIdx = -1;
            let minStopDist = 550;
            calls.forEach((c, idx) => {
                if (c.latitude && c.longitude && train.latitude && train.longitude) {
                    const d = haversineMeters(train.latitude, train.longitude, c.latitude, c.longitude);
                    if (d < minStopDist) {
                        minStopDist = d;
                        closestIdx = idx;
                    }
                }
            });

            const nowMs = Date.now();
            routeLayer.clearLayers();

            const rowsHtml = calls.map((call, idx) => {
                const sched = call.scheduled_departure || call.scheduled_arrival || '';
                const est = call.estimated_departure || call.estimated_arrival || '';
                const schedShort = sched ? sched.substring(11, 16) : '—';
                const estShort = est ? est.substring(11, 16) : '';

                const passedByStatus = call.status === 'actual';
                const passedByTime = est && Date.parse(est) < nowMs - 20000;
                const passedByGeo = closestIdx >= 0 && idx <= closestIdx;
                const isPassed = passedByStatus || passedByTime || passedByGeo;

                let rowStyle = 'background:#ffffff;';
                let delayMin = 0;

                if (isPassed) {
                    if (sched && est) {
                        delayMin = Math.round((Date.parse(est) - Date.parse(sched)) / 60000);
                    } else {
                        delayMin = Math.round((train.delay_s || 0) / 60);
                    }
                    rowStyle = getPassedRowStyle(delayMin);
                }

                if (call.latitude && call.longitude) {
                    const circleColor = isPassed ? getDelayColor(delayMin * 60) : '#ffffff';
                    const strokeColor = isPassed ? '#0f172a' : '#2563eb';
                    const circle = L.circleMarker([call.latitude, call.longitude], {
                        radius: 5,
                        fillColor: circleColor,
                        color: strokeColor,
                        weight: 2,
                        fillOpacity: 1
                    }).bindTooltip(`${call.name} (${schedShort})`);
                    circle.on('click', (e) => {
                        L.DomEvent.stopPropagation(e);
                        openStationDepartures(call.location_id, call.name);
                    });
                    circle.addTo(routeLayer);
                }

                const checkMark = isPassed ? '<span style="font-weight:700;margin-right:4px;">✓</span>' : '';
                const actualDisplay = isPassed
                    ? `<strong>${estShort || schedShort}</strong> <span style="font-size:11px;">(${delayMin > 0 ? '+' + delayMin + 'm' : 'včas'})</span>`
                    : (estShort && estShort !== schedShort ? `<span style="color:#64748b;">~${estShort}</span>` : '—');

                return `
                    <tr style="${rowStyle}">
                        <td>${checkMark}<a class="stop-link" data-stop-id="${call.location_id}" data-stop-name="${call.name}">${call.name}</a></td>
                        <td>${schedShort}</td>
                        <td>${actualDisplay}</td>
                    </tr>
                `;
            }).join('');

            container.innerHTML = `
                <div class="detail-box">
                    <div class="detail-header">
                        <h3 class="detail-title">${trip.trip_short_name || train.trip_short_name} → ${trip.headsign || train.headsign}</h3>
                        <div class="detail-meta">
                            Dopravce: <strong>${trip.agency || 'Neznámý'}</strong><br>
                            Aktuální zpoždění: <strong>${formatDelayText(train.delay_s)}</strong>
                        </div>
                        <div class="detail-actions">
                            <button id="toggle-follow-btn" class="follow-btn ${followSelectedTrain ? 'active' : ''}">
                                ${followSelectedTrain ? '📍 Sledování ZAPNUTO' : '📍 Sledovat vlak'}
                            </button>
                            <button id="deselect-train-btn" class="deselect-btn" title="Odoznačit vlak (nebo klikněte do mapy)">
                                ✕ Odoznačit
                            </button>
                        </div>
                    </div>
                    ${szAlertsHtml}
                    ${alertsHtml}
                    ${turnaroundHtml}
                    <table class="timetable-table">
                        <thead>
                            <tr>
                                <th>Stanice / zastávka</th>
                                <th>Plán</th>
                                <th>Skutečnost</th>
                            </tr>
                        </thead>
                        <tbody>${rowsHtml}</tbody>
                    </table>
                </div>
            `;

            if (silent) {
                container.scrollTop = prevScrollTop;
            }

            document.getElementById('toggle-follow-btn').addEventListener('click', (e) => {
                followSelectedTrain = !followSelectedTrain;
                e.target.classList.toggle('active', followSelectedTrain);
                e.target.textContent = followSelectedTrain ? '📍 Sledování ZAPNUTO' : '📍 Sledovat vlak';
                if (followSelectedTrain && clientVehiclesCache[selectedVehicleId]) {
                    const t = clientVehiclesCache[selectedVehicleId].data;
                    map.panTo([t.latitude, t.longitude], { animate: true });
                    openTrainDetail(t, true);
                }
            });

            document.getElementById('deselect-train-btn').addEventListener('click', () => {
                clearSelectedTrain();
            });

            container.querySelectorAll('.stop-link').forEach(link => {
                link.addEventListener('click', () => {
                    openStationDepartures(link.dataset.stopId, link.dataset.stopName);
                });
            });
        } catch (err) {
            if (!silent) {
                container.innerHTML = `<div class="empty-state">Nepodařilo se načíst detail spoje.</div>`;
            }
        } finally {
            detailRefreshInFlight = false;
        }
    }

    async function openStationDepartures(stopId, stopName) {
        if (!stopId) return;
        switchTab('station');
        const container = document.getElementById('tab-station');
        container.innerHTML = `<div class="empty-state">Načítám odjezdovou tabuli stanice <strong>${stopName}</strong>...</div>`;

        try {
            const res = await fetch(`/api/departures?stop_id=${encodeURIComponent(stopId)}`);
            const data = await res.json();

            const railDeps = (data.departures || []).filter(d => d.line?.mode === 'rail');
            const rowsHtml = railDeps.map(d => {
                const sched = d.scheduled ? d.scheduled.substring(11, 16) : '—';
                const delayMin = d.delay_s !== null && d.delay_s !== undefined ? Math.round(d.delay_s / 60) : null;
                const color = getDelayColor(d.delay_s);
                const followed = d.vehicle?.followed_as ? `<div style="font-size:11px;color:#2563eb;">Přechází na jiný spoj</div>` : '';
                return `
                    <tr>
                        <td><strong>${d.trip_short_name || d.line?.short_name || 'Vlak'}</strong>${followed}</td>
                        <td>${d.headsign || '—'}</td>
                        <td>${d.platform || '—'}</td>
                        <td>${sched}</td>
                        <td style="text-align:right;">
                            <span class="delay-pill" style="background:${color};">${delayMin !== null ? (delayMin > 0 ? '+' + delayMin + 'm' : 'včas') : '—'}</span>
                        </td>
                    </tr>
                `;
            }).join('');

            container.innerHTML = `
                <div class="detail-box">
                    <div class="detail-header">
                        <h3 class="detail-title">🚉 ${stopName}</h3>
                        <div class="detail-meta">Odjezdy vlaků v nejbližších 90 minutách</div>
                    </div>
                    <table class="timetable-table">
                        <thead>
                            <tr>
                                <th>Vlak</th>
                                <th>Směr</th>
                                <th>Nást.</th>
                                <th>Čas</th>
                                <th style="text-align:right;">Zpoždění</th>
                            </tr>
                        </thead>
                        <tbody>${rowsHtml || '<tr><td colspan="5" style="text-align:center;color:#64748b;">Žádné odjezdy vlaků</td></tr>'}</tbody>
                    </table>
                </div>
            `;
        } catch (err) {
            container.innerHTML = `<div class="empty-state">Odjezdovou tabuli stanice se nepodařilo načíst.</div>`;
        }
    }

    async function syncViewportAndFetch() {
        const b = map.getBounds();
        const bbox = [
            b.getWest().toFixed(4),
            b.getSouth().toFixed(4),
            b.getEast().toFixed(4),
            b.getNorth().toFixed(4)
        ].join(',');

        try {
            const res = await fetch(`/api/vehicles?bbox=${encodeURIComponent(bbox)}&_=${Date.now()}`, {
                cache: 'no-store'
            });
            const data = await res.json();
            const now = Date.now();

            (data.vehicles || []).forEach(v => {
                if (v.vehicle_id) {
                    clientVehiclesCache[v.vehicle_id] = { data: v, seen: now };
                }
            });

            Object.keys(clientVehiclesCache).forEach(vid => {
                if (now - clientVehiclesCache[vid].seen > 60000) {
                    delete clientVehiclesCache[vid];
                }
            });

            renderAll();
        } catch (e) {
            console.error(e);
        }
    }

    ['carrier-filter', 'category-filter', 'delay-filter'].forEach(id => {
        document.getElementById(id).addEventListener('change', renderAll);
    });
    document.getElementById('search-input').addEventListener('input', renderAll);
    map.on('moveend zoomend', syncViewportAndFetch);

    syncViewportAndFetch();
    setInterval(syncViewportAndFetch, 2500);
            const szLayer = L.layerGroup().addTo(map);
    const szBtn = document.createElement('button');
    szBtn.style.cssText = 'background:#f59e0b;color:#0f172a;border:none;border-radius:7px;padding:5px 9px;font-size:12px;font-weight:700;cursor:pointer;white-space:nowrap;';
    szBtn.textContent = '⚠️ SŽ: 0';
    const statsBar = document.querySelector('.stats-bar');
    if (statsBar) statsBar.prepend(szBtn);

    let szIncidents = new Array();
        function findSzIncidentsForTrain(trainName) {
        if (!trainName) return new Array();
        const normTarget = trainName.toLowerCase().replace(/\s+/g, ' ').trim();
        return szIncidents.filter(inc =>
            (inc.trains || new Array()).some(t => {
                const nt = t.toLowerCase().replace(/\s+/g, ' ').trim();
                return nt === normTarget || nt.startsWith(normTarget + ' ') || normTarget.startsWith(nt + ' ');
            })
        );
    }


    async function loadSzIncidents() {
        try {
            const res = await fetch('/api/sz-incidents?_=' + Date.now(), { cache: 'no-store' });
            const data = await res.json();
            szIncidents = data.incidents || new Array();
            szBtn.textContent = '⚠️ SŽ: ' + szIncidents.length;
            szLayer.clearLayers();

            szIncidents.forEach(inc => {
                const isStopped = inc.impact_code === 2 || (inc.impact || '').toLowerCase().includes('zastaven');
                const color = isStopped ? '#dc2626' : '#f59e0b';
                const trainsTxt = (inc.trains && inc.trains.length) ? '<br><strong>Vlaky:</strong> ' + inc.trains.join(', ') : '';
                const popupHtml = `<div style="font-size:12.5px;max-width:260px;"><strong style="color:${color}">⚠️ ${inc.title}</strong><br><strong>Úsek:</strong> ${inc.start_station} – ${inc.end_station}<br><strong>Dopad:</strong> ${inc.impact}<br><strong>Předpoklad konce:</strong> ${inc.end_time || 'neuvedeno'}${trainsTxt}</div>`;

                (inc.lines || new Array()).forEach(pts => {
                    if (pts.length > 1) {
                        L.polyline(pts, {
                            color: color,
                            weight: 7,
                            opacity: 0.9,
                            dashArray: isStopped ? null : '10, 6'
                        }).bindPopup(popupHtml).addTo(szLayer);
                    }
                });

                if (inc.center && inc.center.length === 2) {
                    const icon = L.divIcon({
                        className: '',
                        html: `<div style="width:26px;height:26px;border-radius:50%;background:${color};border:2px solid #fff;color:#fff;font-weight:800;display:flex;align-items:center;justify-content:center;box-shadow:0 2px 8px rgba(0,0,0,0.5);">!</div>`,
                        iconSize: new Array(26, 26),
                        iconAnchor: new Array(13, 13)
                    });
                    L.marker(inc.center, { icon }).bindPopup(popupHtml).addTo(szLayer);
                }
            });
        } catch (e) {
            console.error('Chyba vykresleni SZ:', e);
        }
    }

        szBtn.addEventListener('click', () => {
        console.log('Data SZ:', szIncidents);
        if (!szIncidents.length) {
            alert('Aktuálně nejsou hlášeny žádné mimořádnosti Správy železnic.');
            return;
        }
        const first = szIncidents.at(0);
        if (first && first.center) {
            map.setView(first.center, 12);
        }
        const list = szIncidents.map((x, i) => `${i + 1}. ${x.title} (${x.start_station}${x.end_station ? ' – ' + x.end_station : ''}) [${x.impact}]`).join('\n');
        alert('Aktivní mimořádnosti Správy železnic:\n\n' + list);
    });


    loadSzIncidents();
    setInterval(loadSzIncidents, 30000);


</script>
</body>
</html>
"""


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


class VlakInfoHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def send_bytes(self, status_code, content_type, body_bytes):
        try:
            self.send_response(status_code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body_bytes)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body_bytes)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            pass

    def send_json(self, status_code, obj):
        self.send_bytes(status_code, "application/json; charset=utf-8", json.dumps(obj).encode("utf-8"))

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            self.send_bytes(200, "text/html; charset=utf-8", HTML_PAGE.encode("utf-8"))
            return

        if path == "/favicon.ico":
            self.send_bytes(204, "image/x-icon", b"")
            return

        if path == "/api/sz-incidents":
            with SZ_LOCK:
                incidents = list(LATEST_SZ_INCIDENTS)
            self.send_json(200, {"incidents": incidents})
            return

        if path == "/api/vehicles":
            bbox_raw = qs.get("bbox", [""])[0]
            try:
                parts = [float(x) for x in bbox_raw.split(",")]
                if len(parts) == 4:
                    with VEHICLES_LOCK:
                        CURRENT_VIEWPORT[:] = parts
                        need_immediate = (time.time() - LAST_FETCH_TS) > 3.0
                    if need_immediate:
                        refresh_vehicles_once()
            except ValueError:
                pass

            with VEHICLES_LOCK:
                vehicles = [item["data"] for item in LATEST_VEHICLES.values()]
            self.send_json(200, {"vehicles": vehicles})
            return

        if path == "/api/trip":
            trip_id = qs.get("trip_id", [""])[0]
            date_str = qs.get("date", [""])[0]
            if not trip_id or not date_str:
                self.send_json(400, {"error": "Missing trip_id or date"})
                return

            cache_key = f"{trip_id}|{date_str}"
            now = time.time()
            with CACHE_LOCK:
                cached = TRIP_CACHE.get(cache_key)
                if cached and (now - cached["ts"] < 12.0):
                    self.send_json(200, cached["data"])
                    return

            try:
                encoded_trip = urllib.parse.quote(trip_id, safe="")
                url = f"{UPSTREAM_BASE}/trips/{encoded_trip}/{urllib.parse.quote(date_str, safe='')}"
                data = fetch_json(url, timeout=10)
                with CACHE_LOCK:
                    TRIP_CACHE[cache_key] = {"ts": now, "data": data}
                self.send_json(200, data)
            except Exception as e:
                self.send_json(502, {"error": str(e)})
            return

        if path == "/api/departures":
            stop_id = qs.get("stop_id", [""])[0]
            if not stop_id:
                self.send_json(400, {"error": "Missing stop_id"})
                return

            now = time.time()
            with CACHE_LOCK:
                cached = DEPARTURES_CACHE.get(stop_id)
                if cached and (now - cached["ts"] < 20.0):
                    self.send_json(200, cached["data"])
                    return

            try:
                encoded_stop = urllib.parse.quote(stop_id, safe="")
                url = f"{UPSTREAM_BASE}/stop-places/{encoded_stop}/departures?limit=40&minutes=90&group=true"
                data = fetch_json(url, timeout=10)
                with CACHE_LOCK:
                    DEPARTURES_CACHE[stop_id] = {"ts": now, "data": data}
                self.send_json(200, data)
            except Exception as e:
                self.send_json(502, {"error": str(e)})
            return

        self.send_json(404, {"error": "Not found"})


def main():
    parser = argparse.ArgumentParser(description="VlakInfo LIVE Server")
    parser.add_argument("--host", default="0.0.0.0", help="Adresa naslouchani (vychozi: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080, help="Port serveru (vychozi: 8080)")
    args = parser.parse_args()

    t = threading.Thread(target=background_poller, daemon=True)
    t.start()

    server = QuietThreadingHTTPServer((args.host, args.port), VlakInfoHandler)
    print(f"VlakInfo LIVE bezi na http://{args.host}:{args.port} (lokalne http://127.0.0.1:{args.port})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
