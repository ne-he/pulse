"""US EPA AQI conversion + health categories for PM2.5.

Lets the dashboard show real, color-coded health bands ("Unhealthy", etc.)
instead of raw µg/m³ numbers — the local, human-meaningful framing recruiters
and users actually read."""
from __future__ import annotations

# (C_low, C_high, AQI_low, AQI_high) breakpoints for PM2.5 (24h, µg/m³), US EPA.
_PM25_BREAKPOINTS = [
    (0.0, 12.0, 0, 50),
    (12.1, 35.4, 51, 100),
    (35.5, 55.4, 101, 150),
    (55.5, 150.4, 151, 200),
    (150.5, 250.4, 201, 300),
    (250.5, 350.4, 301, 400),
    (350.5, 500.4, 401, 500),
]

# Category metadata — name + hex color the dashboard can use directly.
CATEGORIES = [
    (0, 50, "Good", "#00E400"),
    (51, 100, "Moderate", "#FFFF00"),
    (101, 150, "Unhealthy (Sensitive)", "#FF7E00"),
    (151, 200, "Unhealthy", "#FF0000"),
    (201, 300, "Very Unhealthy", "#8F3F97"),
    (301, 500, "Hazardous", "#7E0023"),
]


def pm25_to_aqi(pm25: float) -> int:
    """Convert a PM2.5 concentration (µg/m³) to a US AQI integer."""
    pm25 = max(0.0, float(pm25))
    for c_low, c_high, a_low, a_high in _PM25_BREAKPOINTS:
        if c_low <= pm25 <= c_high:
            aqi = (a_high - a_low) / (c_high - c_low) * (pm25 - c_low) + a_low
            return round(aqi)
    return 500  # off the top of the scale


def aqi_category(aqi: int) -> tuple[str, str]:
    """Return (category_name, hex_color) for an AQI value."""
    for lo, hi, name, color in CATEGORIES:
        if lo <= aqi <= hi:
            return name, color
    return "Hazardous", "#7E0023"


def category_for_pm25(pm25: float) -> tuple[str, str]:
    return aqi_category(pm25_to_aqi(pm25))
