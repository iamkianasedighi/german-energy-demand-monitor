from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_START = "2025-11-01"
DEFAULT_END = "2026-11-01"  # Exclusive Berlin date
API_ROOT = "https://www.smard.de/app/chart_data"
REGION = "DE"
RESOLUTION = "hour"
SERIES = {
    "grid_load": 410,
    "solar": 4068,
    "wind_onshore": 4067,
    "wind_offshore": 1225,
    "residual_official": 4359,
    "price_eur_mwh": 4169,
}
CORE = ("grid_load", "solar", "wind_onshore", "wind_offshore")
