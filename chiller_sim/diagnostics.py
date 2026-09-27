from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

import numpy as np
import pandas as pd

from chiller_sim.model import (
    SimulationConfig,
    saturation_temperature_c,
)


LABEL_COLUMNS = (
    "fault_severity",
    "condenser_ua_kw_per_k",
    "fault_active",
)

COMPARISON_SIGNALS = (
    "ua_cond_est_kw_per_k",
    "condenser_approach_c",
    "cond_sat_temp_c",
    "discharge_pressure_kpa",
    "cw_supply_temp_c",
    "cw_return_temp_c",
    "cw_range_c",
    "compressor_power_kw",
    "cop",
    "suction_pressure_kpa",
    "chw_supply_temp_c",
    "cooling_load_kw",
    "cw_flow_kg_s",
    "outdoor_wet_bulb_c",
)

TREND_SIGNALS = COMPARISON_SIGNALS[:9]


@dataclass(frozen=True)
class HypothesisCheck:
    name: str
    expected: str
    consistent: bool
    note: str


@dataclass(frozen=True)
class Diagnosis:
    degraded: bool
    mechanism: str
    evidence: dict[str, float]
    hypotheses: tuple[HypothesisCheck, ...]


def derive_signals(
    df: pd.DataFrame, config: SimulationConfig = SimulationConfig()
) -> pd.DataFrame:
    sensors = df.drop(columns=list(LABEL_COLUMNS), errors="ignore").copy()
    cw_range = sensors["cw_return_temp_c"] - sensors["cw_supply_temp_c"]
    cond_sat = saturation_temperature_c(sensors["discharge_pressure_kpa"])
    evap_sat = saturation_temperature_c(sensors["suction_pressure_kpa"])
    condenser_heat = (
        sensors["cw_flow_kg_s"] * config.cp_kj_per_kg_k * cw_range
    )
    delta_hot = cond_sat - sensors["cw_supply_temp_c"]
    delta_cold = cond_sat - sensors["cw_return_temp_c"]
    valid_lmtd = cond_sat > sensors["cw_return_temp_c"]
    lmtd = np.full(len(sensors), np.nan, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        lmtd[valid_lmtd] = cw_range[valid_lmtd] * 1.0 / np.log(
            delta_hot[valid_lmtd] / delta_cold[valid_lmtd]
        )

    sensors["cw_range_c"] = cw_range
    sensors["cond_sat_temp_c"] = cond_sat
    sensors["evap_sat_temp_c"] = evap_sat
    sensors["condenser_approach_c"] = (
        cond_sat - sensors["cw_return_temp_c"]
    )
    sensors["lift_c"] = cond_sat - evap_sat
    sensors["condenser_heat_kw"] = condenser_heat
    sensors["ua_cond_est_kw_per_k"] = condenser_heat / lmtd
    return sensors


def _hourly_profile(
    df: pd.DataFrame, signal: str, mask: pd.Series
) -> pd.Series:
    selected = df.loc[mask, ["timestamp", signal]].copy()
    return selected.groupby(selected["timestamp"].dt.hour)[signal].mean()


def compare_periods(
    df: pd.DataFrame,
    config: SimulationConfig = SimulationConfig(),
    degraded_hours: int = 24,
) -> pd.DataFrame:
    sensors = df.drop(columns=list(LABEL_COLUMNS), errors="ignore").copy()
    derived = derive_signals(sensors, config)
    timestamps = pd.to_datetime(derived["timestamp"])
    healthy = timestamps < pd.Timestamp(config.fault_start)
    degraded = timestamps >= timestamps.max() - pd.Timedelta(hours=degraded_hours)

    rows = []
    for signal in COMPARISON_SIGNALS:
        healthy_hourly = _hourly_profile(derived, signal, healthy)
        degraded_hourly = _hourly_profile(derived, signal, degraded)
        matched = pd.concat(
            [healthy_hourly.rename("healthy"), degraded_hourly.rename("degraded")],
            axis=1,
            join="inner",
        ).dropna()
        healthy_mean = float(matched["healthy"].mean())
        degraded_mean = float(matched["degraded"].mean())
        delta = degraded_mean - healthy_mean
        pct_change = 100.0 * delta / healthy_mean if healthy_mean != 0 else np.nan
        rows.append(
            {
                "signal": signal,
                "healthy_mean": healthy_mean,
                "degraded_mean": degraded_mean,
                "delta": delta,
                "pct_change": pct_change,
            }
        )
    return pd.DataFrame(rows).set_index("signal")


def _slope_for_window(
    hourly: pd.DataFrame, signals: tuple[str, ...], mask: pd.Series
) -> dict[str, float]:
    window = hourly.loc[mask].copy()
    elapsed_days = (
        (window.index - window.index[0]).total_seconds().to_numpy() / 86400.0
    )
    design = np.column_stack(
        [
            np.ones(len(window)),
            elapsed_days,
            window["cooling_load_kw"].to_numpy(),
            window["outdoor_wet_bulb_c"].to_numpy(),
        ]
    )
    slopes = {}
    for signal in signals:
        response = window[signal].to_numpy()
        valid = np.isfinite(design).all(axis=1) & np.isfinite(response)
        coefficients, *_ = np.linalg.lstsq(design[valid], response[valid], rcond=None)
        slopes[signal] = float(coefficients[1])
    return slopes


def trend_slopes(
    df: pd.DataFrame, config: SimulationConfig = SimulationConfig()
) -> pd.DataFrame:
    sensors = df.drop(columns=list(LABEL_COLUMNS), errors="ignore").copy()
    derived = derive_signals(sensors, config)
    hourly = (
        derived.set_index("timestamp")
        .resample("1h")
        .mean(numeric_only=True)
    )
    healthy_mask = hourly.index < pd.Timestamp(config.fault_start)
    fault_mask = hourly.index >= pd.Timestamp(config.fault_start)
    healthy_slopes = _slope_for_window(hourly, TREND_SIGNALS, healthy_mask)
    fault_slopes = _slope_for_window(hourly, TREND_SIGNALS, fault_mask)
    return pd.DataFrame(
        {
            "healthy_slope_per_day": pd.Series(healthy_slopes),
            "fault_slope_per_day": pd.Series(fault_slopes),
        },
        index=TREND_SIGNALS,
    )


def diagnose(comparison: pd.DataFrame) -> Diagnosis:
    deltas = comparison["delta"].astype(float).to_dict()
    relative = (comparison["pct_change"] / 100.0).astype(float).to_dict()
    condenser_consistent = (
        relative["ua_cond_est_kw_per_k"] <= -0.10
        and deltas["condenser_approach_c"] >= 1.0
        and deltas["discharge_pressure_kpa"] >= 20.0
        and deltas["condenser_approach_c"]
        > 2.0 * abs(deltas["cw_supply_temp_c"])
        and abs(relative["cw_flow_kg_s"]) < 0.02
        and abs(deltas["suction_pressure_kpa"]) < 10.0
        and abs(relative["cooling_load_kw"]) < 0.06
    )
    degraded = (
        relative["ua_cond_est_kw_per_k"] <= -0.10
        or deltas["condenser_approach_c"] >= 1.0
    )

    hypotheses = (
        HypothesisCheck(
            "Condenser heat-transfer degradation",
            "UA estimate decreases while condenser approach and discharge rise",
            condenser_consistent,
            "Consistent with waterside fouling or scaling."
            if condenser_consistent
            else "The combined condenser signature is incomplete.",
        ),
        HypothesisCheck(
            "Elevated CW supply / tower-weather",
            "CW supply rises with condensing saturation temperature",
            deltas["cw_supply_temp_c"] >= 0.5 * deltas["cond_sat_temp_c"]
            and deltas["condenser_approach_c"] < 1.0,
            "The CW supply change accounts for much of the condensing-temperature change."
            if deltas["cw_supply_temp_c"] >= 0.5 * deltas["cond_sat_temp_c"]
            and deltas["condenser_approach_c"] < 1.0
            else "The observed changes do not match a tower-weather-only signature.",
        ),
        HypothesisCheck(
            "Reduced CW flow",
            "Condenser-water flow decreases",
            relative["cw_flow_kg_s"] <= -0.05,
            "CW flow decreased by at least 5%."
            if relative["cw_flow_kg_s"] <= -0.05
            else "Measured CW flow did not decrease by at least 5%.",
        ),
        HypothesisCheck(
            "Evaporator-side fault or refrigerant undercharge",
            "Suction pressure decreases",
            deltas["suction_pressure_kpa"] <= -10.0,
            "Suction pressure fell by at least 10 kPa."
            if deltas["suction_pressure_kpa"] <= -10.0
            else "Suction pressure did not fall by at least 10 kPa.",
        ),
        HypothesisCheck(
            "Increased cooling load",
            "Cooling load increases by at least 6%",
            relative["cooling_load_kw"] >= 0.06,
            "Cooling load increased by at least 6%."
            if relative["cooling_load_kw"] >= 0.06
            else "Cooling load did not increase by at least 6%.",
        ),
        HypothesisCheck(
            "Non-condensables in the condenser",
            "Condenser-side pressure and approach rise with apparent UA loss",
            condenser_consistent,
            "These sensors can't tell it apart from fouling; distinguishing it "
            "would need a refrigerant liquid temperature vs. saturation check, "
            "or purge history.",
        ),
    )
    if condenser_consistent:
        mechanism = (
            "condenser heat-transfer degradation (waterside fouling/scaling)"
        )
    elif not degraded:
        mechanism = "no significant degradation"
    else:
        mechanism = "unexplained degradation"

    used_signals = (
        "ua_cond_est_kw_per_k",
        "condenser_approach_c",
        "discharge_pressure_kpa",
        "cw_supply_temp_c",
        "cw_flow_kg_s",
        "suction_pressure_kpa",
        "cooling_load_kw",
        "cond_sat_temp_c",
    )
    evidence = {}
    for signal in used_signals:
        evidence[f"{signal}_delta"] = float(deltas[signal])
        evidence[f"{signal}_relative_change"] = float(relative[signal])
    return Diagnosis(degraded, mechanism, evidence, hypotheses)


def _format_three(value: float) -> str:
    rounded = Decimal(str(float(value))).quantize(
        Decimal("0.001"), rounding=ROUND_HALF_UP
    )
    return f"{rounded:.3f}"


def measured_changes_markdown(
    comparison: pd.DataFrame, trends: pd.DataFrame
) -> str:
    headers = (
        "signal",
        "healthy",
        "degraded",
        "Δ",
        "Δ%",
        "healthy slope/day",
        "fault slope/day",
    )
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for signal, row in comparison.iterrows():
        healthy_slope = (
            trends.loc[signal, "healthy_slope_per_day"]
            if signal in trends.index
            else np.nan
        )
        fault_slope = (
            trends.loc[signal, "fault_slope_per_day"]
            if signal in trends.index
            else np.nan
        )
        values = (
            str(signal),
            _format_three(row["healthy_mean"]),
            _format_three(row["degraded_mean"]),
            _format_three(row["delta"]),
            _format_three(row["pct_change"]) + "%",
            _format_three(healthy_slope) if pd.notna(healthy_slope) else "—",
            _format_three(fault_slope) if pd.notna(fault_slope) else "—",
        )
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)
