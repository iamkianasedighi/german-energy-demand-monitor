"""Independent, uncached SMARD-to-SQLite audit for the selected SMARD reporting period.

This script does not import the project's ETL or use its downloaded cache.
Requires: requests (already installed in the project's .venv).
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from math import isclose, isfinite
from pathlib import Path
import sqlite3
import sys
from time import sleep
from zoneinfo import ZoneInfo

import requests


SERIES = {
    "grid_load": (410, "DE"),
    "solar": (4068, "DE"),
    "wind_onshore": (4067, "DE"),
    "wind_offshore": (1225, "DE"),
    "residual_official": (4359, "DE"),
    "price_eur_mwh": (4169, "DE-LU"),
}
BASE = "https://www.smard.de/app/chart_data"
TOLERANCE = 0.000001


def get_fresh(session: requests.Session, url: str, output_file: Path) -> tuple[dict, dict]:
    """Always issue a new HTTP request and retain evidence of the response."""
    last_error = None
    for attempt in range(4):
        try:
            response = session.get(url, timeout=35, headers={"Cache-Control": "no-cache"})
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Unexpected JSON root")
            output_file.parent.mkdir(parents=True, exist_ok=True)
            output_file.write_bytes(response.content)
            evidence = {
                "url": url,
                "http_status": response.status_code,
                "sha256": sha256(response.content).hexdigest(),
                "bytes": len(response.content),
            }
            return data, evidence
        except (requests.RequestException, ValueError) as error:
            last_error = error
            if attempt < 3:
                sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Could not download {url}: {last_error}")


def utc_hours(start: str, end: str) -> list[int]:
    berlin = ZoneInfo("Europe/Berlin")
    start_utc = datetime.fromisoformat(start).replace(tzinfo=berlin).astimezone(timezone.utc)
    end_utc = datetime.fromisoformat(end).replace(tzinfo=berlin).astimezone(timezone.utc)
    if end_utc <= start_utc:
        raise ValueError("End must be after start")
    total = int((end_utc - start_utc).total_seconds() // 3600)
    if (end_utc - start_utc).total_seconds() != total * 3600:
        raise ValueError("Window must consist of complete hours")
    first = int(start_utc.timestamp()) * 1000
    return [first + 3600000 * hour for hour in range(total)]


def download_series(session: requests.Session, name: str, code: int, region: str,
                    hours: list[int], output: Path) -> tuple[dict[int, float | None], list[dict]]:
    base = f"{BASE}/{code}/{region}"
    index_url = f"{base}/index_hour.json"
    index, evidence = get_fresh(session, index_url, output / "downloads" / name / "index_hour.json")
    stamps = index.get("timestamps")
    if not isinstance(stamps, list) or not stamps:
        raise ValueError(f"Invalid weekly index for {name}")
    stamps = sorted(set(int(stamp) for stamp in stamps))
    # Each index timestamp begins a weekly file. Include the week preceding the start.
    candidates = [stamp for stamp in stamps if stamp <= hours[0]]
    if not candidates:
        raise ValueError(f"No weekly file contains the first requested hour for {name}")
    begin = candidates[-1]
    selected = [stamp for stamp in stamps if begin <= stamp <= hours[-1]]
    observed: dict[int, float | None] = {}
    manifests = [evidence]
    expected = set(hours)
    for number, stamp in enumerate(selected, 1):
        url = f"{base}/{code}_{region}_hour_{stamp}.json"
        payload, manifest = get_fresh(session, url, output / "downloads" / name / f"{stamp}.json")
        entries = payload.get("series")
        if not isinstance(entries, list):
            raise ValueError(f"Malformed weekly data for {name} at {stamp}")
        manifest["series_rows"] = len(entries)
        manifests.append(manifest)
        for pair in entries:
            if not isinstance(pair, list) or len(pair) != 2:
                raise ValueError(f"Invalid reading in {name} at {stamp}")
            ts, value = int(pair[0]), pair[1]
            if ts not in expected:
                continue
            val = None if value is None else float(value)
            if val is not None and not isfinite(val):
                raise ValueError(f"Non-finite data in {name} at {ts}")
            if ts in observed and observed[ts] != val:
                raise ValueError(f"Conflicting overlapping SMARD windows: {name} at {ts}")
            observed[ts] = val
        if number % 10 == 0 or number == len(selected):
            print(f"  {name}: downloaded {number}/{len(selected)} weekly files", flush=True)
    return observed, manifests


def prior_values(conn: sqlite3.Connection) -> dict[tuple[str, int], float | None]:
    return {(metric, int(timestamp)): value for timestamp, metric, value in conn.execute(
        "SELECT timestamp_ms, metric, value FROM raw_readings"
    )}


def compare(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return isclose(a, b, rel_tol=0, abs_tol=TOLERANCE)


def q(values: list[float], frac: float) -> float:
    items = sorted(values)
    pos = (len(items) - 1) * frac
    low = int(pos)
    high = min(low + 1, len(items) - 1)
    return items[low] + (items[high] - items[low]) * (pos - low)


def verify_derived(conn: sqlite3.Connection, fresh: dict[tuple[str, int], float | None],
                   hours: list[int], mismatches: list[list]) -> int:
    from_db = {int(row[0]): row for row in conn.execute(
        "SELECT timestamp_ms, local_date, local_hour, weekday, month, wind_solar_mwh, "
        "residual_calc_mwh, residual_gap_mwh, complete FROM hourly_metrics"
    )}
    berlin = ZoneInfo("Europe/Berlin")
    failures = 0
    for ts in hours:
        row = from_db.get(ts)
        values = [fresh.get((name, ts)) for name in ("grid_load", "solar", "wind_onshore", "wind_offshore")]
        solar_wind = None if any(v is None for v in values[1:]) else sum(values[1:])
        residual = None if solar_wind is None or values[0] is None else values[0] - solar_wind
        official = fresh.get(("residual_official", ts))
        gap = None if residual is None or official is None else residual - official
        complete = int(all(v is not None for v in values))
        local = datetime.fromtimestamp(ts / 1000, timezone.utc).astimezone(berlin)
        expected = [local.strftime("%Y-%m-%d"), local.hour, local.weekday(),
                    local.strftime("%Y-%m"), solar_wind, residual, gap, complete]
        if row is None:
            mismatches.append(["derived", ts, "missing_hourly_record", "", ""])
            failures += 1
            continue
        for name, actual, want in zip(("local_date", "local_hour", "weekday", "month",
                                       "wind_solar_mwh", "residual_calc_mwh", "residual_gap_mwh",
                                       "complete"), row[1:], expected):
            ok = compare(actual, want) if name in ("wind_solar_mwh", "residual_calc_mwh", "residual_gap_mwh") else actual == want
            if not ok:
                mismatches.append(["derived", ts, name, actual, want])
                failures += 1
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Fresh independent download and audit of six SMARD hourly series")
    parser.add_argument("--db", type=Path, default=Path("outputs/energy.sqlite"))
    parser.add_argument("--start", default="2025-11-01")
    parser.add_argument("--end", default="2026-11-01")
    parser.add_argument("--out", type=Path, default=Path("outputs/smard_independent_audit"))
    args = parser.parse_args()
    if not args.db.is_file():
        print(f"Existing SQLite file not found: {args.db}", file=sys.stderr)
        return 1
    hours = utc_hours(args.start, args.end)
    args.out.mkdir(parents=True, exist_ok=True)
    print(f"Re-downloading six series for {len(hours):,} UTC hours; no project cache is used.", flush=True)
    fresh: dict[tuple[str, int], float | None] = {}
    evidence: list[dict] = []
    coverage: dict[str, dict] = {}
    with requests.Session() as session:
        session.headers.update({"User-Agent": "GermanEnergyPortfolioIndependentAudit/1.0", "Accept": "application/json"})
        for name, (code, region) in SERIES.items():
            print(f"Downloading {name} ({code}/{region})", flush=True)
            downloaded, manifests = download_series(session, name, code, region, hours, args.out)
            fresh.update({(name, ts): value for ts, value in downloaded.items()})
            missing = [ts for ts in hours if ts not in downloaded]
            nulls = sum(downloaded.get(ts) is None for ts in hours if ts in downloaded)
            coverage[name] = {"records": len(downloaded), "missing_timestamps": len(missing),
                              "null_readings": nulls, "weekly_files": len(manifests) - 1}
            evidence.extend(manifests)
    mismatches: list[list] = []
    with sqlite3.connect(args.db) as conn:
        original = prior_values(conn)
        for name in SERIES:
            for ts in hours:
                key = (name, ts)
                if key not in fresh or key not in original:
                    mismatches.append(["source", ts, name, original.get(key, "MISSING"), fresh.get(key, "MISSING")])
                    continue
                if not compare(original[key], fresh[key]):
                    mismatches.append(["source", ts, name, original[key], fresh[key]])
        old_in_window = {(name, ts) for name, ts in original if hours[0] <= ts <= hours[-1] and name in SERIES}
        extra = old_in_window.difference(fresh)
        for name, ts in sorted(extra):
            if not any(m[0] == "source" and m[1] == ts and m[2] == name for m in mismatches):
                mismatches.append(["source", ts, name, original[name, ts], "MISSING"])
        derived_failures = verify_derived(conn, fresh, hours, mismatches)
    with (args.out / "differences.csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["section", "timestamp_ms", "metric_or_column", "stored", "fresh"])
        writer.writerows(mismatches)
    (args.out / "download_manifest.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    residual = {}
    price = {}
    for ts in hours:
        load = fresh.get(("grid_load", ts))
        renewables = [fresh.get((name, ts)) for name in ("solar", "wind_onshore", "wind_offshore")]
        if load is not None and all(x is not None for x in renewables):
            residual[ts] = load - sum(renewables)
        p = fresh.get(("price_eur_mwh", ts))
        if p is not None:
            price[ts] = p
    negative = [ts for ts, p in price.items() if p < 0]
    quartile = q(list(residual.values()), .25) if residual else None
    negative_in_low = (sum(ts in residual and residual[ts] <= quartile for ts in negative)
                       if quartile is not None else None)
    summary = {
        "verdict": "PASS" if not mismatches and all(v["missing_timestamps"] == 0 for v in coverage.values()) else "REVIEW_REQUIRED",
        "source": "Bundesnetzagentur SMARD; fetched live without project cache",
        "run_utc": datetime.now(timezone.utc).isoformat(),
        "source_period": {"start": args.start, "end_exclusive": args.end},
        "existing_sqlite": str(args.db.resolve()),
        "hour_count": len(hours),
        "expected_source_rows": len(hours) * len(SERIES),
        "fresh_source_rows": len(fresh),
        "compared_source_rows": len(original),
        "coverage": coverage,
        "source_and_derived_differences": len(mismatches),
        "derived_differences": derived_failures,
        "negative_price_hours_from_fresh_data": len(negative),
        "negative_price_hours_in_bottom_residual_quartile": negative_in_low,
        "residual_quartile_gw": (quartile / 1000 if quartile is not None else None),
        "evidence_files": len(evidence),
        "float_abs_tolerance": TOLERANCE,
    }
    (args.out / "audit_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Audit verdict: {summary['verdict']}; {len(mismatches)} differences")
    print(f"Evidence: {args.out / 'audit_summary.json'}")
    if mismatches:
        print(f"Review differences: {args.out / 'differences.csv'}")
    return 0 if summary["verdict"] == "PASS" else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, ValueError, requests.RequestException) as exc:
        print(f"AUDIT DID NOT COMPLETE: {exc}", file=sys.stderr)
        sys.exit(1)
