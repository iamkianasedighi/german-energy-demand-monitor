from bisect import bisect_right
from contextlib import closing
from datetime import date
from pathlib import Path
import json
import os
import sqlite3

import numpy as np
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import API_ROOT, CORE, REGION, RESOLUTION, SERIES


FIELDS = ["timestamp_ms", "local_date", "local_hour", "weekday", "month", *SERIES,
          "wind_solar_mwh", "residual_calc_mwh", "residual_gap_mwh", "complete"]


def date_window(start: str, end: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    try:
        if len(start) != 10 or len(end) != 10:
            raise ValueError("Expected YYYY-MM-DD dates")
        first = pd.Timestamp(date.fromisoformat(start)).tz_localize("Europe/Berlin").tz_convert("UTC")
        last = pd.Timestamp(date.fromisoformat(end)).tz_localize("Europe/Berlin").tz_convert("UTC")
    except (TypeError, ValueError) as exc:
        raise ValueError("Use YYYY-MM-DD Berlin calendar dates") from exc
    if first >= last:
        raise ValueError("End must be after start")
    return first, last


def session_with_retries() -> requests.Session:
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=0.6, status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def get_json(session, url: str, cache_file: Path | None = None, refresh: bool = False):
    if cache_file and cache_file.exists() and not refresh:
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Damaged cache file: {cache_file}. Use --refresh") from exc
    try:
        response = session.get(url, timeout=25)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(f"SMARD request failed: {url}") from exc
    if cache_file:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        pending = cache_file.with_suffix(".tmp")
        pending.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(pending, cache_file)
    return payload


def fetch_metric(session, name: str, filter_id: int, first: pd.Timestamp,
                 last: pd.Timestamp, cache_dir: Path | None = None,
                 refresh: bool = False) -> list[dict]:
    # Prices refer to the Germany/Luxembourg bidding zone; load and generation refer to Germany.
    region = "DE-LU" if name == "price_eur_mwh" else REGION
    base = f"{API_ROOT}/{filter_id}/{region}"
    index = get_json(session, f"{base}/index_{RESOLUTION}.json")
    if not isinstance(index, dict) or not isinstance(index.get("timestamps"), list):
        raise ValueError(f"Unexpected SMARD index: {name}")
    stamps = sorted({int(s) for s in index["timestamps"]})
    first_ms, last_ms = first.value // 1_000_000, last.value // 1_000_000
    index_position = bisect_right(stamps, first_ms) - 1
    if index_position < 0:
        raise ValueError(f"No SMARD window covering {first}: {name}")
    values = {}
    for stamp in stamps[index_position:]:
        if stamp >= last_ms:
            break
        url = f"{base}/{filter_id}_{region}_{RESOLUTION}_{stamp}.json"
        cached = Path(cache_dir) / name / f"{stamp}.json" if cache_dir else None
        payload = get_json(session, url, cached, refresh)
        if not isinstance(payload, dict) or not isinstance(payload.get("series"), list):
            raise ValueError(f"Unexpected SMARD series: {name}")
        for item in payload["series"]:
            if not isinstance(item, list) or len(item) != 2:
                raise ValueError(f"Malformed observation: {name}")
            timestamp, value = int(item[0]), item[1]
            if not first_ms <= timestamp < last_ms:
                continue
            if timestamp % 3_600_000:
                raise ValueError(f"Not an hourly timestamp: {timestamp}")
            number = None if value is None else float(value)
            if number is not None and not np.isfinite(number):
                raise ValueError(f"Non-finite reading: {name}")
            if name in CORE and number is not None and number < 0:
                raise ValueError(f"Negative load or generation: {name}")
            if timestamp in values and values[timestamp] != number:
                raise ValueError(f"Conflicting readings at {timestamp}: {name}")
            values[timestamp] = number
    if not values:
        raise ValueError(f"No observations returned: {name}")
    return [{"timestamp_ms": ts, "metric": name, "value": val}
            for ts, val in sorted(values.items())]


def collect(start: str, end: str, cache_dir: Path | None = None,
            refresh: bool = False):
    first, last = date_window(start, end)
    with session_with_retries() as session:
        rows = []
        for metric, filter_id in SERIES.items():
            rows.extend(fetch_metric(session, metric, filter_id, first, last, cache_dir, refresh))
    return pd.DataFrame(rows), first, last


def transform(raw: pd.DataFrame, first: pd.Timestamp, last: pd.Timestamp) -> pd.DataFrame:
    if not {"timestamp_ms", "metric", "value"}.issubset(raw.columns):
        raise ValueError("Source columns missing")
    if raw.empty or raw.duplicated(["timestamp_ms", "metric"]).any():
        raise ValueError("Empty input or duplicate timestamp/metric")
    if not set(raw["metric"]).issubset(SERIES):
        raise ValueError("Unknown source metric")
    frame = raw.copy()
    frame["timestamp_ms"] = pd.to_numeric(frame["timestamp_ms"], errors="raise")
    frame["value"] = pd.to_numeric(frame["value"], errors="raise")
    if (not np.isfinite(frame["timestamp_ms"]).all() or
            (frame["timestamp_ms"] % 3_600_000).any()):
        raise ValueError("Invalid hourly timestamps")
    if (np.isinf(frame["value"]).any() or
            frame.loc[frame["metric"].isin(CORE), "value"].lt(0).any()):
        raise ValueError("Non-finite or negative readings")
    frame["timestamp_utc"] = pd.to_datetime(frame["timestamp_ms"], unit="ms", utc=True)
    if frame["timestamp_utc"].lt(first).any() or frame["timestamp_utc"].ge(last).any():
        raise ValueError("Out-of-window reading")
    wide = frame.pivot(index="timestamp_utc", columns="metric", values="value")
    hours = pd.date_range(first, last, freq="h", inclusive="left")
    wide = wide.reindex(index=hours, columns=list(SERIES))
    result = wide.rename_axis("timestamp_utc").reset_index()
    result["wind_solar_mwh"] = result[list(CORE[1:])].sum(axis=1, min_count=3)
    result["residual_calc_mwh"] = result["grid_load"] - result["wind_solar_mwh"]
    result["residual_gap_mwh"] = result["residual_calc_mwh"] - result["residual_official"]
    result["complete"] = result[list(CORE)].notna().all(axis=1).astype(int)
    local = result["timestamp_utc"].dt.tz_convert("Europe/Berlin")
    result["timestamp_ms"] = result["timestamp_utc"].astype("int64") // 1_000_000
    result["local_date"] = local.dt.strftime("%Y-%m-%d")
    result["local_hour"] = local.dt.hour
    result["weekday"] = local.dt.weekday
    result["month"] = local.dt.strftime("%Y-%m")
    return result


SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_readings (
    timestamp_ms INTEGER NOT NULL, metric TEXT NOT NULL, value REAL,
    PRIMARY KEY(timestamp_ms, metric));
CREATE TABLE IF NOT EXISTS hourly_metrics (
    timestamp_ms INTEGER PRIMARY KEY, local_date TEXT NOT NULL,
    local_hour INTEGER NOT NULL, weekday INTEGER NOT NULL, month TEXT NOT NULL,
    grid_load REAL, solar REAL, wind_onshore REAL, wind_offshore REAL,
    residual_official REAL, price_eur_mwh REAL, wind_solar_mwh REAL,
    residual_calc_mwh REAL, residual_gap_mwh REAL,
    complete INTEGER NOT NULL CHECK(complete IN (0,1)));
CREATE INDEX IF NOT EXISTS ix_hourly_date ON hourly_metrics(local_date);
"""


def _insert(conn: sqlite3.Connection, raw: pd.DataFrame, hourly: pd.DataFrame):
    rows = raw[["timestamp_ms", "metric", "value"]]
    conn.executemany("INSERT OR REPLACE INTO raw_readings VALUES (?, ?, ?)",
                     rows.astype(object).where(pd.notna(rows), None).itertuples(index=False, name=None))
    values = hourly[FIELDS].astype(object).where(pd.notna(hourly[FIELDS]), None)
    placeholders = ",".join("?" for _ in FIELDS)
    conn.executemany(f"INSERT OR REPLACE INTO hourly_metrics VALUES ({placeholders})",
                     values.itertuples(index=False, name=None))


def save_sqlite(raw: pd.DataFrame, hourly: pd.DataFrame, path: Path,
                update: bool = False):
    path.parent.mkdir(parents=True, exist_ok=True)
    if update and path.exists():
        with closing(sqlite3.connect(path)) as conn, conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] != 3:
                raise ValueError("Old database format: run without --update to rebuild")
            _insert(conn, raw, hourly)
        return
    pending = path.with_suffix(".pending")
    pending.unlink(missing_ok=True)
    try:
        with closing(sqlite3.connect(pending)) as conn, conn:
            conn.executescript(SCHEMA)
            conn.execute("PRAGMA user_version=3")
            _insert(conn, raw, hourly)
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


def last_saved_hour(path: Path):
    if not path.exists():
        return None
    with closing(sqlite3.connect(path)) as conn:
        if conn.execute("PRAGMA user_version").fetchone()[0] != 3:
            raise ValueError("Old database format: run without --update to rebuild")
        saved = conn.execute("SELECT MAX(timestamp_ms) FROM hourly_metrics").fetchone()[0]
    return pd.to_datetime(saved, unit="ms", utc=True) if saved is not None else None


def read_sqlite(path: Path, first: pd.Timestamp | None = None,
                last: pd.Timestamp | None = None) -> pd.DataFrame:
    sql = "SELECT * FROM hourly_metrics"
    values = []
    if first is not None and last is not None:
        sql += " WHERE timestamp_ms >= ? AND timestamp_ms < ?"
        values = [first.value // 1_000_000, last.value // 1_000_000]
    sql += " ORDER BY timestamp_ms"
    with closing(sqlite3.connect(path)) as conn:
        return pd.read_sql_query(sql, conn, params=values)
