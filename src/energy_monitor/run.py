import argparse
import json
import logging
from datetime import datetime, timezone

import pandas as pd

from .analysis import quality_summary
from .charts import save_charts
from .config import DEFAULT_END, DEFAULT_START, ROOT
from .etl import (collect, date_window, last_saved_hour, read_sqlite,
                  save_sqlite, transform)
from .report import write_report


log = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="German energy and day-ahead price analysis")
    parser.add_argument("--start", default=DEFAULT_START, help="First Berlin date YYYY-MM-DD")
    parser.add_argument("--end", default=DEFAULT_END, help="Exclusive Berlin date YYYY-MM-DD")
    parser.add_argument("--refresh", action="store_true", help="Refetch SMARD weekly files")
    parser.add_argument("--update", action="store_true", help="Upsert recent hours into an existing database")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    first, last = date_window(args.start, args.end)
    output = ROOT / "outputs"
    database = output / "energy.sqlite"
    fetch_start = args.start
    previous = last_saved_hour(database) if args.update else None
    if previous is not None:
        overlap = (previous - pd.Timedelta(days=7)).tz_convert("Europe/Berlin")
        fetch_start = max(args.start, overlap.strftime("%Y-%m-%d"))
        log.info("Incremental update from %s, including recent source revisions", fetch_start)
    raw, start, end = collect(fetch_start, args.end, ROOT / "data" / "cache",
                              args.refresh or args.update)
    changed = transform(raw, start, end)
    save_sqlite(raw, changed, database, update=args.update)
    full = read_sqlite(database, first, last)
    quality = quality_summary(full)
    expected = len(pd.date_range(first, last, freq="h", inclusive="left"))
    if len(full) != expected:
        raise ValueError(f"Incomplete database hour spine: {len(full)} of {expected} hours")
    coverage = quality["complete_hours"] / quality["expected_hours"]
    if coverage < 0.98:
        raise ValueError(f"Core source coverage {coverage:.1%} is below 98%")
    log.info("Stored %s hours; load/generation coverage %.2f%%", f"{len(full):,}", coverage * 100)
    price_cover = 1 - quality["missing_by_metric"]["price_eur_mwh"] / len(full)
    log.info("Day-ahead price coverage %.2f%%", price_cover * 100)
    _, days = write_report(full, args.start, args.end, output / "report.md")
    save_charts(full, days, output / "figures")
    columns = ["grid_load", "solar", "wind_onshore", "wind_offshore", "residual_official", "price_eur_mwh"]
    full[columns].isna().groupby(full["local_date"]).sum().to_csv(output / "data_quality.csv")
    (output / "run_metadata.json").write_text(json.dumps({
        "start": args.start, "end_exclusive": args.end,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "source": "SMARD/Bundesnetzagentur", "source_observations_fetched": len(raw),
        "hours_in_database": len(full), "complete_days": len(days),
        "price_coverage": round(price_cover, 6), "incremental": args.update,
    }, indent=2) + "\n", encoding="utf-8")
    log.info("Report: %s", output / "report.md")
    log.info("Charts: %s", output / "figures")


if __name__ == "__main__":
    main()
