import numpy as np
import pandas as pd
from scipy import stats

from .config import CORE, SERIES

SEASONS = {12: "Winter", 1: "Winter", 2: "Winter",
           3: "Spring", 4: "Spring", 5: "Spring",
           6: "Summer", 7: "Summer", 8: "Summer",
           9: "Autumn", 10: "Autumn", 11: "Autumn"}


def quality_summary(hourly: pd.DataFrame) -> dict:
    comparable = hourly["residual_gap_mwh"].notna() & hourly["grid_load"].gt(0)
    bad = comparable & (hourly["residual_gap_mwh"].abs() / hourly["grid_load"] > 0.02)
    return {"expected_hours": len(hourly), "complete_hours": int(hourly["complete"].sum()),
            "missing_by_metric": {name: int(hourly[name].isna().sum()) for name in SERIES},
            "residual_discrepancies": int(bad.sum()),
            "residual_comparable_hours": int(comparable.sum())}


def daily_summary(hourly: pd.DataFrame) -> pd.DataFrame:
    days = hourly.groupby("local_date", as_index=False).agg(
        observed_hours=("complete", "size"), complete_hours=("complete", "sum"),
        peak_grid_mwh=("grid_load", "max"), peak_residual_mwh=("residual_calc_mwh", "max"),
        grid_energy_mwh=("grid_load", "sum"), wind_solar_mwh=("wind_solar_mwh", "sum"),
    )
    # The UTC hour spine already accounts for local 23- and 25-hour days.
    return days.loc[days["complete_hours"] == days["observed_hours"]].copy()


def monthly_summary(days: pd.DataFrame) -> pd.DataFrame:
    frame = days.copy()
    frame["month"] = frame["local_date"].str[:7]
    return frame.groupby("month", as_index=False).agg(
        mean_daily_grid_gwh=("grid_energy_mwh", lambda x: x.mean() / 1000),
        mean_daily_wind_solar_gwh=("wind_solar_mwh", lambda x: x.mean() / 1000),
        complete_days=("local_date", "size"),
    )


def residual_opportunities(hourly: pd.DataFrame):
    good = hourly.loc[hourly["complete"].eq(1)].copy()
    if good.empty:
        raise ValueError("No complete hours")
    cutoff = float(good["residual_calc_mwh"].quantile(0.1))
    good["low_residual"] = good["residual_calc_mwh"] <= cutoff
    good["season"] = pd.to_datetime(good["local_date"]).dt.month.map(SEASONS)
    season_hour = good.groupby(["season", "local_hour"], as_index=False).agg(
        low_rate=("low_residual", "mean"),
        median_residual_gw=("residual_calc_mwh", lambda x: x.median() / 1000),
        observations=("low_residual", "size"),
    )
    monthly = good.groupby("month", as_index=False).agg(
        low_hours=("low_residual", "sum"), observed=("low_residual", "size"))
    return season_hour, monthly, cutoff


def price_analysis(hourly: pd.DataFrame) -> dict | None:
    valid = hourly.loc[hourly["complete"].eq(1) & hourly["price_eur_mwh"].notna()].copy()
    if len(valid) < 500 or valid["price_eur_mwh"].notna().mean() == 0:
        return None
    cutoff = float(valid["residual_calc_mwh"].quantile(0.25))
    valid["low"] = valid["residual_calc_mwh"] <= cutoff
    valid["negative"] = valid["price_eur_mwh"] < 0
    low = valid.loc[valid["low"]]
    rest = valid.loc[~valid["low"]]
    if len(low) < 100 or len(rest) < 100:
        return None

    # Resample whole local days rather than treating consecutive hours as independent.
    counts = valid.groupby(["local_date", "low"]).agg(
        n=("negative", "size"), negative=("negative", "sum")
    ).unstack("low", fill_value=0)
    if len(counts) < 30:
        return None
    n_low = counts[("n", True)].to_numpy()
    n_rest = counts[("n", False)].to_numpy()
    neg_low = counts[("negative", True)].to_numpy()
    neg_rest = counts[("negative", False)].to_numpy()
    rng = np.random.default_rng(2405)
    picks = rng.integers(0, len(counts), size=(2000, len(counts)))
    denominators_low = n_low[picks].sum(axis=1)
    denominators_rest = n_rest[picks].sum(axis=1)
    good = (denominators_low > 0) & (denominators_rest > 0)
    boot = (neg_low[picks][good].sum(axis=1) / denominators_low[good] -
            neg_rest[picks][good].sum(axis=1) / denominators_rest[good])
    if len(boot) < 100:
        return None

    hourly_medians = valid.groupby("local_hour")["price_eur_mwh"].median()
    cheapest_hour = int(hourly_medians.idxmin())
    priciest_hour = int(hourly_medians.idxmax())
    cheap = valid.loc[valid["local_hour"] == cheapest_hour,
                      ["local_date", "price_eur_mwh"]].rename(
                          columns={"price_eur_mwh": "cheap_price"})
    expensive = valid.loc[valid["local_hour"] == priciest_hour,
                          ["local_date", "price_eur_mwh"]].rename(
                              columns={"price_eur_mwh": "expensive_price"})
    paired = cheap.merge(expensive, on="local_date")
    spreads = paired["expensive_price"] - paired["cheap_price"]
    rho = stats.spearmanr(valid["residual_calc_mwh"], valid["price_eur_mwh"]).statistic
    return {
        "hours": len(valid), "price_coverage": len(valid) / int(hourly["complete"].sum()),
        "negative_hours": int(valid["negative"].sum()),
        "negative_hours_in_low_quartile": int(low["negative"].sum()),
        "low_residual_cutoff_gw": cutoff / 1000,
        "low_negative_rate": float(low["negative"].mean()),
        "rest_negative_rate": float(rest["negative"].mean()),
        "rate_gap": float(low["negative"].mean() - rest["negative"].mean()),
        "rate_gap_ci": np.quantile(boot, [0.025, 0.975]).tolist(),
        "spearman": float(rho), "matched_days": len(spreads),
        "median_price_spread": float(spreads.median()) if len(spreads) else None,
        "positive_spread_share": float(spreads.gt(0).mean()) if len(spreads) else None,
        "lowest_median_price_hour": cheapest_hour,
        "highest_median_price_hour": priciest_hour,
    }
