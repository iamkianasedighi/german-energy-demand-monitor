from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd

from .analysis import monthly_summary, price_analysis, residual_opportunities


def save_charts(hourly: pd.DataFrame, days: pd.DataFrame, figures: Path):
    figures.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"figure.figsize": (9, 4.5), "axes.spines.top": False,
                         "axes.spines.right": False, "savefig.dpi": 150})

    fig, ax = plt.subplots()
    x = pd.to_datetime(days["local_date"])
    ax.plot(x, days["peak_grid_mwh"] / 1000, label="Grid load", linewidth=1.1)
    ax.plot(x, days["peak_residual_mwh"] / 1000, label="Residual load", linewidth=1.1)
    locator = mdates.AutoDateLocator(minticks=4, maxticks=8)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    ax.set(title="Daily peak grid and residual load", ylabel="GW")
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(figures / "01_daily_peaks.png")
    plt.close(fig)

    by_season, _, cutoff = residual_opportunities(hourly)
    seasons = ["Winter", "Spring", "Summer", "Autumn"]
    matrix = by_season.pivot(index="season", columns="local_hour", values="low_rate").reindex(seasons)
    fig, ax = plt.subplots(figsize=(9, 3.8))
    chart = ax.imshow(matrix.to_numpy() * 100, aspect="auto", cmap="Blues", vmin=0)
    ax.set(yticks=np.arange(4), yticklabels=seasons,
           xticks=np.arange(0, 24, 2), xticklabels=np.arange(0, 24, 2),
           xlabel="Berlin local hour", title=f"Low residual load by season (≤{cutoff / 1000:.1f} GW)")
    fig.colorbar(chart, ax=ax, label="Share of hours at this clock time (%)", shrink=0.85)
    fig.tight_layout()
    fig.savefig(figures / "02_seasonal_low_hours.png")
    plt.close(fig)

    months = monthly_summary(days)
    x = np.arange(len(months))
    fig, ax = plt.subplots(figsize=(10, 4.5))
    ax.bar(x - 0.2, months["mean_daily_grid_gwh"], 0.4, label="Grid load")
    ax.bar(x + 0.2, months["mean_daily_wind_solar_gwh"], 0.4,
           label="Wind + solar generation")
    ax.set(xticks=x, xticklabels=months["month"],
           title="Mean energy per complete local day", ylabel="GWh/day")
    ax.tick_params(axis="x", rotation=45)
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(figures / "03_monthly_energy.png")
    plt.close(fig)

    price = price_analysis(hourly)
    fig, ax = plt.subplots()
    if price and price["price_coverage"] >= 0.95:
        valid = hourly.loc[hourly["complete"].eq(1) & hourly["price_eur_mwh"].notna()]
        x = valid["residual_calc_mwh"].to_numpy() / 1000
        y = valid["price_eur_mwh"].to_numpy()
        scatter = ax.hexbin(x, y, gridsize=42, mincnt=1, cmap="Blues")
        fig.colorbar(scatter, ax=ax, label="Hourly observations")
        ax.axhline(0, linestyle="--", linewidth=1, color="black", alpha=0.6)
        ax.set(title="Day-ahead price and residual load", xlabel="Residual load (GW)",
               ylabel="Day-ahead price (€/MWh)")
    else:
        for season in ["Winter", "Spring", "Summer", "Autumn"]:
            series = by_season.loc[by_season["season"] == season].sort_values("local_hour")
            ax.plot(series["local_hour"], series["median_residual_gw"], label=season)
        ax.set(xticks=np.arange(0, 24, 2), xlabel="Berlin local hour",
               ylabel="Median residual load (GW)",
               title="Residual-load profile by season (prices not included)")
        ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(figures / "04_market_context.png")
    plt.close(fig)
