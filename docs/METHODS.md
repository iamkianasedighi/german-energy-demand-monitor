# Methods

## Data and units

The study covers 1 November 2025 to 31 October 2026. SMARD's German load, solar, onshore wind, offshore wind and published residual-load series are combined with DE-LU day-ahead wholesale prices. The pipeline uses hourly UTC keys so the clock change in Berlin produces 23- and 25-hour local days without duplicate database keys.

SMARD's hourly generation and load readings are MWh per hour-long interval. Dividing by 1,000 gives the equivalent average **GW**. Aggregating a day's readings and dividing by 1,000 gives **GWh/day**. Day-ahead prices are in **€/MWh**; for this period an hourly day-ahead value is the average of four 15-minute values.

Calculated residual load is `grid_load - solar - wind_onshore - wind_offshore`. The separately published residual-load series is used to check the calculation. Residual load is **not** a direct measurement of conventional generation or grid stress.

## Checks and missing data

The import rejects duplicated timestamps/metric keys, malformed timestamps, infinite values and negative load or renewable generation. Negative prices are valid data. Missing measurements remain null; a legitimate zero is retained. A full hourly UTC sequence is preserved even when a source value is missing. Source completeness must be at least 98% for load/generation. Differences between calculated and published residual load above 2% of grid load are flagged. The monthly GWh/day chart uses only complete local days to avoid comparing raw totals from unequal month lengths.

For the committed reporting period, the independently re-downloaded six SMARD series matched **52,560 stored source values** over **8,760 hours**, with **zero differences**. See [source_audit.json](../results/2025-11_to_2026-10/source_audit.json); the audit can be rerun with `python scripts/verify_smard_independently.py` after creating a local SQLite database.

## Statistics

Two different residual-load cutoffs answer different questions:

- **Bottom 10% (≤3.5 GW)** is used for the seasonal hour-of-day heatmap. Each cell shows the proportion of that season's observations at that clock hour falling below one full-period cutoff; it does not indicate the share of all low hours.
- **Bottom 25% (≤16.9 GW)** is used to compare negative-price rates with the remaining matched hours. This larger group gives a useful descriptive rate denominator. In the observed year, all 462 negative-price hours were in the bottom quartile; 410 of those were also in the bottom decile.

The difference in negative-price frequency is **21.1 percentage points**, not a 21.1% relative increase. A 95% percentile interval comes from 2,000 bootstrap resamples of **whole Berlin-local dates** with a fixed seed. This retains within-day clustering but does not fully address dependence between days, seasonality or other confounders. Spearman's rho (0.69) describes rank association between price and residual load, not causation.

For the descriptive time-of-day comparison, the code selects the clock hours with the **lowest and highest median prices** in the analyzed period, pairs prices on the same Berlin-local date, and takes the median of the daily differences. In this dataset those hours are **13:00 and 20:00**; the median paired spread is about **€116/MWh**, with 20:00 more expensive on approximately **90%** of paired days. The hours are selected retrospectively from the same year, so the result should not be treated as an out-of-sample expected price spread.

Actual savings would depend on contractual pricing, losses, flexibility, and operating constraints. SMARD observations and revisions can change; use `--refresh` to rebuild the period and `--update` for later published or revised hours. The seven-day overlap is intentional because a previously unfilled hour can later acquire a reading.
