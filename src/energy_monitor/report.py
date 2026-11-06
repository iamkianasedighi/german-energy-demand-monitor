from pathlib import Path

from .analysis import (daily_summary, monthly_summary, price_analysis,
                       quality_summary, residual_opportunities)


def write_report(hourly, start: str, end: str, destination: Path):
    quality = quality_summary(hourly)
    days = daily_summary(hourly)
    if days.empty:
        raise ValueError("No complete Berlin-local days")
    hourly_season, monthly_low, low_cutoff = residual_opportunities(hourly)
    monthly = monthly_summary(days)
    prices = price_analysis(hourly)
    coverage = quality["complete_hours"] / quality["expected_hours"]

    season_lines = []
    for season in ["Winter", "Spring", "Summer", "Autumn"]:
        data = hourly_season.loc[hourly_season["season"] == season]
        top = data.nlargest(3, "low_rate")
        hours = ", ".join(f"{int(row.local_hour):02d}:00 ({row.low_rate:.0%})"
                          for row in top.itertuples())
        season_lines.append(f"| {season} | {hours} |")
    monthly_lines = [f"| {r.month} | {r.mean_daily_grid_gwh:,.1f} | "
                     f"{r.mean_daily_wind_solar_gwh:,.1f} | {int(r.complete_days)} |"
                     for r in monthly.itertuples()]
    low_lines = [f"| {r.month} | {int(r.low_hours)} | {int(r.observed)} |"
                 for r in monthly_low.itertuples()]

    if prices and prices["price_coverage"] >= 0.95:
        ci = prices["rate_gap_ci"]
        low_hour = prices["lowest_median_price_hour"]
        high_hour = prices["highest_median_price_hour"]
        scenario = (f"Across **{prices['matched_days']}** days with prices at both hours, "
                    f"the median {high_hour:02d}:00 minus {low_hour:02d}:00 "
                    f"price difference was **approximately €{prices['median_price_spread']:.0f}/MWh** "
                    f"({high_hour:02d}:00 was more expensive on "
                    f"{prices['positive_spread_share']:.0%} of those days). "
                    "The hours are selected as the highest and lowest median-price "
                    "clock hours in this observed period.")
        price_section = f"""## Wholesale-price findings

- Valid matched price and load hours: **{prices['hours']:,}** ({prices['price_coverage']:.1%} of complete-load hours).
- Negative day-ahead price hours: **{prices['negative_hours']:,}**;
  **{prices['negative_hours_in_low_quartile']:,}** fell in the lowest residual-load quartile.
- In the lowest residual-load quartile (≤{prices['low_residual_cutoff_gw']:.1f} GW),
  negative-price frequency: **{prices['low_negative_rate']:.1%}**; remaining hours:
  **{prices['rest_negative_rate']:.1%}**.
- Rate difference: **{prices['rate_gap'] * 100:+.1f} percentage points**
  (95% day-block bootstrap interval
  **[{ci[0] * 100:+.1f}, {ci[1] * 100:+.1f}] percentage points**).
- Descriptive Spearman correlation of residual load and price: **{prices['spearman']:.2f}**.

{scenario}

The wholesale-price spread is not a realized saving; tariff terms and operational flexibility determine whether shifting demand is useful.
"""
        recommendation = ("A flexible electricity consumer could examine whether some "
                          "operations can be moved to historically lower-priced midday hours. "
                          "Check its tariff, equipment constraints and forward prices before acting.")
    else:
        price_section = """## Wholesale-price findings

Day-ahead prices are unavailable or below the 95% coverage check for this snapshot.
Run the six-series SMARD pipeline to add market-price comparisons.
"""
        recommendation = ("The residual-load patterns identify hours worth examining, but "
                          "a pricing recommendation requires matched day-ahead prices.")

    body = f"""# German electricity demand, renewables and wholesale prices

**Period:** {start} to {end} (exclusive end, Berlin time)  
**Data:** Bundesnetzagentur SMARD, German load and generation (DE) and DE-LU day-ahead prices

## Decision

{recommendation}

## Main findings

- The lowest **10%** of observed residual-load hours are at or below **{low_cutoff / 1000:.1f} GW**.
  The heatmap uses this threshold to highlight seasonal and hourly patterns.
  The price comparison uses the broader lowest **25%** to retain enough hours for a rate comparison.
- Highest observed grid load in any hour: **{days['peak_grid_mwh'].max() / 1000:.1f} GW**.
- Source coverage: **{quality['complete_hours']:,}/{quality['expected_hours']:,}** complete
  load/generation hours (**{coverage:.1%}**); **{len(days)}** complete Berlin-local days.

## Low-residual-load hours by season

Percentages are calculated among observed hours **at that clock time in that season**.

| Season | Three most frequently low-residual hours |
| --- | --- |
{chr(10).join(season_lines)}

## Monthly context

Monthly values below are normalized per complete day rather than summed over months of different lengths.

| Month | Grid load (GWh/day) | Wind + solar generation (GWh/day) | Complete days |
| --- | ---: | ---: | ---: |
{chr(10).join(monthly_lines)}

Low-residual-load hour counts use the same full-period bottom-decile threshold:

| Month | Low hours | Observed core hours |
| --- | ---: | ---: |
{chr(10).join(low_lines)}

{price_section}
## Data checks

- Missing measurements: {', '.join(f'{key}={num}' for key, num in quality['missing_by_metric'].items())}.
- Reconstructed vs published residual differs by more than 2% of grid load in
  **{quality['residual_discrepancies']}/{quality['residual_comparable_hours']}** comparable hours.
- Missing source values remain missing; genuine zeros remain zeros.

## Charts

![Daily peaks](figures/01_daily_peaks.png)

![Low residual load by season and hour](figures/02_seasonal_low_hours.png)

![Mean daily grid energy and renewable generation by month](figures/03_monthly_energy.png)

![{('Day-ahead wholesale price versus residual load' if prices and prices['price_coverage'] >= 0.95 else 'Seasonal residual-load profile') }](figures/04_market_context.png)

## Scope

Residual load here is grid load minus photovoltaic and onshore/offshore wind generation;
this is not a direct measurement of dispatchable supply requirements. Prices are
DE-LU day-ahead wholesale values; since October 2025 the hourly price is the
average of four quarter-hour prices. The analysis describes historical observations
and does not establish causation.

Data source: **Bundesnetzagentur, SMARD** — CC BY 4.0. https://www.smard.de/en/datennutzung
"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(body, encoding="utf-8")
    return quality, days
