from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class SimulationConfig:
    seed: int = 42
    start: str = "2025-07-01 00:00:00"
    duration_days: float = 7.0
    sample_interval_min: int = 5
    fault_start: str = "2025-07-04 00:00:00"
    max_fault_severity: float = 0.5
    rated_capacity_kw: float = 1000.0
    cp_kj_per_kg_k: float = 4.186
    ua_evap_kw_per_k: float = 150.0
    ua_cond_kw_per_k: float = 200.0
    chw_flow_kg_s: float = 40.0
    cw_flow_kg_s: float = 50.0
    chw_supply_setpoint_c: float = 7.0
    tower_approach_c: float = 4.0
    wet_bulb_mean_c: float = 24.0
    wet_bulb_amplitude_c: float = 3.0
    wet_bulb_peak_hour: float = 15.0
    wet_bulb_ar_phi: float = 0.98
    wet_bulb_ar_sd_c: float = 0.3
    load_baseline_kw: float = 650.0
    load_amplitude_kw: float = 200.0
    load_peak_hour: float = 15.0
    load_ar_phi: float = 0.95
    load_ar_sd_kw: float = 25.0
    compressor_carnot_fraction: float = 0.55
    compressor_plr_optimum: float = 0.8
    compressor_plr_curvature: float = 0.3
    temperature_noise_sd_c: float = 0.05
    flow_noise_fraction: float = 0.003
    power_noise_sd_kw: float = 1.0
    pressure_noise_sd_kpa: float = 2.0


REQUIRED_COLUMNS = (
    "timestamp",
    "outdoor_wet_bulb_c",
    "chw_supply_temp_c",
    "chw_return_temp_c",
    "cw_supply_temp_c",
    "cw_return_temp_c",
    "chw_flow_kg_s",
    "cw_flow_kg_s",
    "compressor_power_kw",
    "suction_pressure_kpa",
    "discharge_pressure_kpa",
    "cooling_load_kw",
    "cop",
    "fault_severity",
    "condenser_ua_kw_per_k",
    "fault_active",
)


def _ar1(
    standard_normal: np.ndarray,
    phi: float,
    stationary_sd: float,
) -> np.ndarray:
    values = np.empty(len(standard_normal), dtype=float)
    values[0] = stationary_sd * standard_normal[0]
    innovation_scale = np.sqrt(1.0 - phi**2) * stationary_sd
    for index, innovation in enumerate(standard_normal[1:], start=1):
        values[index] = phi * values[index - 1] + innovation_scale * innovation
    return values


def fault_severity(
    timestamps: pd.DatetimeIndex, config: SimulationConfig
) -> np.ndarray:
    fault_time = pd.Timestamp(config.fault_start)
    last_time = timestamps[-1]
    if last_time <= fault_time:
        return np.where(
            timestamps >= fault_time, config.max_fault_severity, 0.0
        )

    elapsed = (timestamps - fault_time).total_seconds().to_numpy()
    ramp_duration = (last_time - fault_time).total_seconds()
    ramp = np.clip(elapsed / ramp_duration, 0.0, 1.0)
    return config.max_fault_severity * ramp


def generate_telemetry(
    config: SimulationConfig = SimulationConfig(),
) -> pd.DataFrame:
    if config.sample_interval_min <= 0:
        raise ValueError("sample_interval_min must be positive")

    periods = int(
        config.duration_days * 24 * 60 / config.sample_interval_min
    )
    timestamps = pd.date_range(
        start=config.start,
        periods=periods,
        freq=f"{config.sample_interval_min}min",
    )
    if len(timestamps) == 0:
        raise ValueError("simulation must contain at least one timestamp")

    rng = np.random.default_rng(config.seed)
    exogenous_noise = rng.normal(size=(len(timestamps), 2))
    hour = (
        timestamps.hour.to_numpy()
        + timestamps.minute.to_numpy() / 60.0
        + timestamps.second.to_numpy() / 3600.0
    )
    daily_phase = 2.0 * np.pi * (hour - config.wet_bulb_peak_hour) / 24.0

    load_phase = 2.0 * np.pi * (hour - config.load_peak_hour) / 24.0
    cooling_load_true = (
        config.load_baseline_kw
        + config.load_amplitude_kw * np.cos(load_phase)
        + _ar1(
            exogenous_noise[:, 0],
            config.load_ar_phi,
            config.load_ar_sd_kw,
        )
    )
    cooling_load_true = np.clip(cooling_load_true, 200.0, 950.0)
    wet_bulb = (
        config.wet_bulb_mean_c
        + config.wet_bulb_amplitude_c * np.cos(daily_phase)
        + _ar1(
            exogenous_noise[:, 1],
            config.wet_bulb_ar_phi,
            config.wet_bulb_ar_sd_c,
        )
    )
    plr = cooling_load_true / config.rated_capacity_kw
    fault = fault_severity(timestamps, config)
    condenser_ua = config.ua_cond_kw_per_k * (1.0 - fault)

    chw_capacity_rate = config.chw_flow_kg_s * config.cp_kj_per_kg_k
    cw_capacity_rate = config.cw_flow_kg_s * config.cp_kj_per_kg_k
    evaporator_effectiveness = 1.0 - np.exp(
        -config.ua_evap_kw_per_k / chw_capacity_rate
    )
    chw_return_true = (
        config.chw_supply_setpoint_c
        + cooling_load_true / chw_capacity_rate
    )
    evaporator_temp = (
        chw_return_true
        - cooling_load_true
        / (evaporator_effectiveness * chw_capacity_rate)
    )
    cw_supply_true = wet_bulb + config.tower_approach_c
    condenser_effectiveness = 1.0 - np.exp(
        -condenser_ua / cw_capacity_rate
    )
    compressor_efficiency = config.compressor_carnot_fraction * (
        1.0
        - config.compressor_plr_curvature
        * (plr - config.compressor_plr_optimum) ** 2
    )

    compressor_power = cooling_load_true / 5.0
    converged = False
    for _ in range(50):
        condenser_load = cooling_load_true + compressor_power
        condenser_temp = (
            cw_supply_true
            + condenser_load / (condenser_effectiveness * cw_capacity_rate)
        )
        cop_true = (
            compressor_efficiency
            * (evaporator_temp + 273.15)
            / (condenser_temp - evaporator_temp)
        )
        updated_power = cooling_load_true / cop_true
        if np.max(np.abs(updated_power - compressor_power)) < 1e-9:
            compressor_power = updated_power
            converged = True
            break
        compressor_power = updated_power
    if not converged:
        raise RuntimeError("compressor/condenser fixed-point iteration did not converge")

    condenser_load = cooling_load_true + compressor_power
    condenser_temp = (
        cw_supply_true
        + condenser_load / (condenser_effectiveness * cw_capacity_rate)
    )
    cw_return_true = cw_supply_true + condenser_load / cw_capacity_rate
    suction_pressure_true = np.exp(
        15.425 - 2662.0 / (evaporator_temp + 273.15)
    )
    discharge_pressure_true = np.exp(
        15.425 - 2662.0 / (condenser_temp + 273.15)
    )

    # Sensor noise draws: wet bulb, four water temperatures, two flows,
    # compressor power, then suction and discharge pressures.
    wet_bulb_measured = wet_bulb + rng.normal(
        0.0, config.temperature_noise_sd_c, len(timestamps)
    )
    chw_supply_measured = config.chw_supply_setpoint_c + rng.normal(
        0.0, config.temperature_noise_sd_c, len(timestamps)
    )
    chw_return_measured = chw_return_true + rng.normal(
        0.0, config.temperature_noise_sd_c, len(timestamps)
    )
    cw_supply_measured = cw_supply_true + rng.normal(
        0.0, config.temperature_noise_sd_c, len(timestamps)
    )
    cw_return_measured = cw_return_true + rng.normal(
        0.0, config.temperature_noise_sd_c, len(timestamps)
    )
    chw_flow_measured = config.chw_flow_kg_s + rng.normal(
        0.0,
        config.chw_flow_kg_s * config.flow_noise_fraction,
        len(timestamps),
    )
    cw_flow_measured = config.cw_flow_kg_s + rng.normal(
        0.0,
        config.cw_flow_kg_s * config.flow_noise_fraction,
        len(timestamps),
    )
    compressor_power_measured = compressor_power + rng.normal(
        0.0, config.power_noise_sd_kw, len(timestamps)
    )
    suction_pressure_measured = suction_pressure_true + rng.normal(
        0.0, config.pressure_noise_sd_kpa, len(timestamps)
    )
    discharge_pressure_measured = discharge_pressure_true + rng.normal(
        0.0, config.pressure_noise_sd_kpa, len(timestamps)
    )

    measured_cooling_load = (
        chw_flow_measured
        * config.cp_kj_per_kg_k
        * (chw_return_measured - chw_supply_measured)
    )
    measured_cop = measured_cooling_load / compressor_power_measured
    fault_active = (timestamps >= pd.Timestamp(config.fault_start)).astype(int)

    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "outdoor_wet_bulb_c": wet_bulb_measured,
            "chw_supply_temp_c": chw_supply_measured,
            "chw_return_temp_c": chw_return_measured,
            "cw_supply_temp_c": cw_supply_measured,
            "cw_return_temp_c": cw_return_measured,
            "chw_flow_kg_s": chw_flow_measured,
            "cw_flow_kg_s": cw_flow_measured,
            "compressor_power_kw": compressor_power_measured,
            "suction_pressure_kpa": suction_pressure_measured,
            "discharge_pressure_kpa": discharge_pressure_measured,
            "cooling_load_kw": measured_cooling_load,
            "cop": measured_cop,
            "fault_severity": fault,
            "condenser_ua_kw_per_k": condenser_ua,
            "fault_active": fault_active,
        },
        columns=REQUIRED_COLUMNS,
    )


def save_telemetry(df: pd.DataFrame, path: str | Path) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False, float_format="%.4f")
