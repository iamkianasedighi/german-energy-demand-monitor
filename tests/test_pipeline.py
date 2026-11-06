import sqlite3
from contextlib import closing

import numpy as np
import pandas as pd
import pytest

from energy_monitor.analysis import (daily_summary, monthly_summary,
                                     price_analysis, quality_summary,
                                     residual_opportunities)
from energy_monitor.charts import save_charts
from energy_monitor.config import SERIES
from energy_monitor.etl import (date_window, fetch_metric, get_json, last_saved_hour,
                                read_sqlite, save_sqlite, transform)
from energy_monitor.report import write_report


def sample_raw(start="2025-11-01", end="2026-11-01"):
    first, last = date_window(start, end)
    hours = pd.date_range(first, last, freq="h", inclusive="left")
    rows = []
    for utc in hours:
        local = utc.tz_convert("Europe/Berlin")
        hour = local.hour
        load = 55000 + (7000 if local.weekday() < 5 else 0) + 5000 * np.sin(hour / 24 * 2 * np.pi)
        solar = max(0, 15000 * np.sin((hour - 6) * np.pi / 12))
        wind = 10000 + 6000 * np.sin(local.dayofyear / 5)
        residual = load - solar - wind - 3500
        price = 0.006 * (residual - 30000) + (-80 if 11 <= hour <= 15 else 60)
        values = {"grid_load": load, "solar": solar, "wind_onshore": wind,
                  "wind_offshore": 3500, "residual_official": residual,
                  "price_eur_mwh": price}
        for name, value in values.items():
            rows.append({"timestamp_ms": utc.value // 1_000_000, "metric": name, "value": value})
    return pd.DataFrame(rows), first, last


@pytest.fixture(scope="module")
def year_data():
    raw, first, last = sample_raw()
    return raw, transform(raw, first, last), first, last


def test_dst_hour_spine(year_data):
    _, hourly, first, last = year_data
    assert len(pd.date_range(first, last, freq="h", inclusive="left")) == 8760
    assert hourly.loc[hourly["local_date"] == "2026-03-29"].shape[0] == 23
    assert hourly.loc[hourly["local_date"] == "2026-10-25"].shape[0] == 25
    assert len(daily_summary(hourly)) == 365


@pytest.mark.parametrize("start,end", [
    ("2026-01-01", "2026-01-01"), ("2026-11-01", "2026-10-01"),
    ("2026-11-1", "2026-12-01"), ("today", "2026-12-01")
])
def test_invalid_dates(start, end):
    with pytest.raises(ValueError):
        date_window(start, end)


def test_null_and_zero_values(year_data):
    raw, _, first, last = year_data
    modified = raw.copy()
    solar = modified.index[modified["metric"] == "solar"]
    modified.loc[solar[0], "value"] = 0.0
    modified.loc[solar[1], "value"] = np.nan
    result = transform(modified, first, last)
    assert result.loc[0, "solar"] == 0
    assert pd.isna(result.loc[1, "solar"])
    assert result.loc[0, "complete"] == 1
    assert result.loc[1, "complete"] == 0
    assert pd.isna(result.loc[1, "residual_calc_mwh"])
    assert len(daily_summary(result)) == 364


def test_bad_data_rejected(year_data):
    raw, _, first, last = year_data
    with pytest.raises(ValueError, match="duplicate"):
        transform(pd.concat([raw, raw.iloc[[0]]]), first, last)
    for value, word in [(float("inf"), "Non-finite"), (-2, "negative")]:
        invalid = raw.copy()
        invalid.loc[0, "value"] = value
        with pytest.raises(ValueError, match=word):
            transform(invalid, first, last)
    invalid = raw.copy()
    invalid.loc[0, "timestamp_ms"] += 1
    with pytest.raises(ValueError, match="timestamps"):
        transform(invalid, first, last)


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, start_ms):
        self.start_ms = start_ms
        self.calls = []

    def get(self, url, timeout):
        self.calls.append(url)
        if "index_hour" in url:
            return Response({"timestamps": [self.start_ms - 3_600_000,
                                            self.start_ms + 7 * 86_400_000]})
        return Response({"series": [[self.start_ms, -12.2],
                                    [self.start_ms + 3_600_000, None]]})


def test_market_price_endpoint_and_cache(tmp_path):
    first, last = date_window("2026-10-01", "2026-10-02")
    client = FakeSession(first.value // 1_000_000)
    rows = fetch_metric(client, "price_eur_mwh", SERIES["price_eur_mwh"], first, last, tmp_path)
    assert rows[0]["value"] == -12.2
    assert rows[1]["value"] is None
    assert all("/DE-LU/" in url for url in client.calls)
    fetch_metric(client, "price_eur_mwh", SERIES["price_eur_mwh"], first, last, tmp_path)
    assert len(client.calls) == 3
    path = next(tmp_path.rglob("*.json"))
    path.write_text("broken", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Damaged cache"):
        get_json(client, "https://example.com", path)


def test_monthly_and_low_residual(year_data):
    _, hourly, _, _ = year_data
    monthly = monthly_summary(daily_summary(hourly))
    assert len(monthly) == 12
    assert monthly.loc[monthly["month"] == "2026-02", "complete_days"].iloc[0] == 28
    season, count, cutoff = residual_opportunities(hourly)
    assert len(season) == 96
    assert len(count) == 12
    assert count["low_hours"].sum() == pytest.approx(876, abs=2)
    assert np.isfinite(cutoff)


def test_price_statistics_and_uncertainty(year_data):
    _, hourly, _, _ = year_data
    price = price_analysis(hourly)
    assert price["hours"] == 8760
    assert price["low_negative_rate"] > price["rest_negative_rate"]
    assert price["rate_gap_ci"][0] <= price["rate_gap"] <= price["rate_gap_ci"][1]
    assert price["median_price_spread"] > 0
    assert price["negative_hours_in_low_quartile"] <= price["negative_hours"]
    assert 0 <= price["lowest_median_price_hour"] <= 23
    assert price["matched_days"] == 365
    changed = hourly.copy()
    changed.loc[changed["local_hour"] == 2, "price_eur_mwh"] -= 1000
    changed.loc[changed["local_hour"] == 21, "price_eur_mwh"] += 1000
    revised = price_analysis(changed)
    assert revised["lowest_median_price_hour"] == 2
    assert revised["highest_median_price_hour"] == 21


def test_sqlite_update_and_reports(tmp_path, year_data):
    raw, hourly, first, last = year_data
    path = tmp_path / "energy.sqlite"
    half = pd.Timestamp("2026-05-01").tz_localize("Europe/Berlin").tz_convert("UTC")
    half_ms = half.value // 1_000_000
    earlier = raw[raw["timestamp_ms"] < half_ms]
    later = raw[raw["timestamp_ms"] >= half_ms].copy()
    save_sqlite(earlier, transform(earlier, first, half), path)
    assert last_saved_hour(path) < half
    later.loc[later["metric"].eq("price_eur_mwh"), "value"] += 1
    save_sqlite(later, transform(later, half, last), path, update=True)
    stored = read_sqlite(path, first, last)
    assert len(stored) == len(hourly)
    assert quality_summary(stored)["complete_hours"] == 8760
    assert stored["price_eur_mwh"].notna().all()
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM raw_readings").fetchone()[0] == len(raw)
    _, days = write_report(stored, "2025-11-01", "2026-11-01", tmp_path / "report.md")
    save_charts(stored, days, tmp_path / "figures")
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "Wholesale-price findings" in report
    assert "price difference" in report
    assert "percentage points" in report
    assert len(list((tmp_path / "figures").glob("*.png"))) == 4
    assert not path.with_suffix(".pending").exists()



def test_update_fills_late_published_values(tmp_path, year_data):
    raw, _, _, _ = year_data
    first, last = date_window("2026-10-01", "2026-10-03")
    first_ms, last_ms = first.value // 1_000_000, last.value // 1_000_000
    window = raw.loc[raw["timestamp_ms"].between(first_ms, last_ms - 1)].copy()
    delayed_hour = int(window["timestamp_ms"].max())
    delayed = window.copy()
    delayed.loc[(delayed["timestamp_ms"] == delayed_hour) &
                delayed["metric"].isin(["solar", "price_eur_mwh"]), "value"] = np.nan
    database = tmp_path / "energy.sqlite"
    save_sqlite(delayed, transform(delayed, first, last), database)
    before = read_sqlite(database, first, last)
    missing = before.loc[before["timestamp_ms"] == delayed_hour].iloc[0]
    assert missing["complete"] == 0
    assert pd.isna(missing["price_eur_mwh"])

    save_sqlite(window, transform(window, first, last), database, update=True)
    after = read_sqlite(database, first, last)
    repaired = after.loc[after["timestamp_ms"] == delayed_hour].iloc[0]
    assert repaired["complete"] == 1
    assert pd.notna(repaired["solar"])
    assert pd.notna(repaired["price_eur_mwh"])
    assert len(after) == len(before)
    with closing(sqlite3.connect(database)) as conn:
        assert conn.execute(
            "SELECT value FROM raw_readings WHERE timestamp_ms=? AND metric='solar'",
            (delayed_hour,)
        ).fetchone()[0] is not None


def test_rebuild_replaces_on_windows(tmp_path, year_data):
    raw, hourly, _, _ = year_data
    database = tmp_path / "energy.sqlite"
    save_sqlite(raw, hourly, database)
    save_sqlite(raw, hourly, database)
    assert len(read_sqlite(database)) == 8760


def test_sql_monitoring_queries(tmp_path, year_data):
    from pathlib import Path
    raw, hourly, _, _ = year_data
    database = tmp_path / "energy.sqlite"
    save_sqlite(raw, hourly, database)
    sql = (Path(__file__).resolve().parents[1] / "sql" / "analysis.sql").read_text(encoding="utf-8")
    statements = [query.strip() for query in sql.split(";") if query.strip()]
    with closing(sqlite3.connect(database)) as conn:
        monthly = conn.execute(statements[0]).fetchall()
        mismatches = conn.execute(statements[1]).fetchall()
    assert len(monthly) == 12
    assert sum(row[1] for row in monthly) == 8760
    assert mismatches == []
