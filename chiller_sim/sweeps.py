import argparse
from dataclasses import dataclass
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from chiller_sim.diagnostics import derive_signals
from chiller_sim.model import (
    SimulationConfig,
    OperatingPoint,
    saturation_pressure_kpa,
    solve_operating_point,
)
from chiller_sim.separation import interval_gap


REFERENCE_LOAD_KW = 650.0
REFERENCE_CW_SUPPLY_C = 28.0
LOAD_POINTS_KW = np.arange(300.0, 951.0, 50.0)
CW_POINTS_C = np.arange(22.0, 32.01, 1.0)
NCG_EXPONENTS = tuple(round(0.5 * i, 1) for i in range(21))
SWEEP_SEEDS = tuple(range(20))
SAMPLES_PER_POINT = 12

FOULING_UA_KW_PER_K = 100.0
DALTON_UA_KW_PER_K = 200.0
BLANKETED_UA_KW_PER_K = 150.0
BLANKETING_ONLY_UA_KW_PER_K = 100.0
SWEEP_CASES = (
    "condenser_fouling",
    "ncg_dalton",
    "ncg_blanketed",
    "ncg_blanketing_only",
)
NCG_CASES = ("ncg_dalton", "ncg_blanketed")
CASE_INDEX = {case: index for index, case in enumerate(SWEEP_CASES)}

LOAD_ELASTICITY = "approach_load_elasticity"
CW_SENSITIVITY = "approach_cw_sensitivity_pct_per_k"
SUBCOOLING_EXCESS = "apparent_subcooling_excess_c"
SIGNATURE_SPECS = {
    LOAD_ELASTICITY: ("load", "current sensors"),
    CW_SENSITIVITY: ("cw", "current sensors"),
    SUBCOOLING_EXCESS: ("both", "added liquid-temperature sensor"),
}

BEGIN_MARKER = "<!-- BEGIN GENERATED: sweep-separation -->"
END_MARKER = "<!-- END GENERATED: sweep-separation -->"


@dataclass(frozen=True)
class SweepResults:
    point_metrics: pd.DataFrame
    signatures: pd.DataFrame
    expected_point_metrics: pd.DataFrame
    expected_signatures: pd.DataFrame
    separation: pd.DataFrame
    claims: pd.DataFrame
    calibration: pd.DataFrame


@dataclass(frozen=True)
class _Scenario:
    case: str
    exponent: float
    exponent_index: int
    operating_point: OperatingPoint


def _noise_free_point(
    cooling_load_kw: float,
    cw_supply_c: float,
    condenser_ua_kw_per_k: float,
    config: SimulationConfig,
    use_total_pressure: bool = False,
    ncg_partial_pressure_kpa: float = 0.0,
) -> OperatingPoint:
    return solve_operating_point(
        cooling_load_kw,
        cw_supply_c,
        condenser_ua_kw_per_k,
        config,
        use_total_pressure=use_total_pressure,
        ncg_partial_pressure_kpa=ncg_partial_pressure_kpa,
    )


def calibrate_ncg_pressure(
    condenser_ua: float, config: SimulationConfig
) -> float:
    healthy = _noise_free_point(
        REFERENCE_LOAD_KW,
        REFERENCE_CW_SUPPLY_C,
        config.ua_cond_kw_per_k,
        config,
    )
    fouling = _noise_free_point(
        REFERENCE_LOAD_KW,
        REFERENCE_CW_SUPPLY_C,
        FOULING_UA_KW_PER_K,
        config,
    )
    target_excess = float(
        fouling.discharge_pressure_kpa - healthy.discharge_pressure_kpa
    )
    low = 0.0
    high = 1000.0
    for _ in range(80):
        candidate = (low + high) / 2.0
        ncg = _noise_free_point(
            REFERENCE_LOAD_KW,
            REFERENCE_CW_SUPPLY_C,
            condenser_ua,
            config,
            use_total_pressure=True,
            ncg_partial_pressure_kpa=candidate,
        )
        excess = float(
            ncg.discharge_pressure_kpa - healthy.discharge_pressure_kpa
        )
        if excess < target_excess:
            low = candidate
        else:
            high = candidate
        if abs(excess - target_excess) <= 0.001:
            return candidate
    return (low + high) / 2.0


def _calibrations(config: SimulationConfig) -> tuple[dict[str, float], dict[str, float]]:
    pressures = {
        "ncg_dalton": calibrate_ncg_pressure(DALTON_UA_KW_PER_K, config),
        "ncg_blanketed": calibrate_ncg_pressure(BLANKETED_UA_KW_PER_K, config),
    }
    references = {}
    for case, ua in (
        ("ncg_dalton", DALTON_UA_KW_PER_K),
        ("ncg_blanketed", BLANKETED_UA_KW_PER_K),
    ):
        point = _noise_free_point(
            REFERENCE_LOAD_KW,
            REFERENCE_CW_SUPPLY_C,
            ua,
            config,
            use_total_pressure=True,
            ncg_partial_pressure_kpa=pressures[case],
        )
        references[case] = float(
            saturation_pressure_kpa(point.condenser_temp_c)
        )
    return pressures, references


def _calibration_table(
    config: SimulationConfig,
    pressures: dict[str, float],
    references: dict[str, float],
) -> pd.DataFrame:
    rows = []
    case_ua = {
        "condenser_fouling": FOULING_UA_KW_PER_K,
        "ncg_dalton": DALTON_UA_KW_PER_K,
        "ncg_blanketed": BLANKETED_UA_KW_PER_K,
        "ncg_blanketing_only": BLANKETING_ONLY_UA_KW_PER_K,
    }
    for case in SWEEP_CASES:
        ua = case_ua[case]
        ncg_pressure = pressures.get(case, 0.0)
        point = _noise_free_point(
            REFERENCE_LOAD_KW,
            REFERENCE_CW_SUPPLY_C,
            ua,
            config,
            use_total_pressure=case in NCG_CASES or case == "ncg_blanketing_only",
            ncg_partial_pressure_kpa=ncg_pressure,
        )
        rows.append(
            {
                "case": case,
                "condenser_ua_kw_per_k": ua,
                "ncg_partial_pressure_kpa": ncg_pressure,
                "ncg_reference_pressure_kpa": references.get(
                    case, float(saturation_pressure_kpa(point.condenser_temp_c))
                ),
            }
        )
    return pd.DataFrame(rows)


def _scenario_operating_point(
    case: str,
    exponent: float,
    load: np.ndarray,
    cw_supply: np.ndarray,
    config: SimulationConfig,
    pressures: dict[str, float],
    references: dict[str, float],
) -> OperatingPoint:
    if case == "condenser_fouling":
        return solve_operating_point(
            load, cw_supply, FOULING_UA_KW_PER_K, config
        )
    if case == "ncg_dalton":
        return solve_operating_point(
            load,
            cw_supply,
            DALTON_UA_KW_PER_K,
            config,
            use_total_pressure=True,
            ncg_partial_pressure_kpa=pressures[case],
            ncg_pressure_exponent=exponent,
            ncg_reference_pressure_kpa=references[case],
        )
    if case == "ncg_blanketed":
        return solve_operating_point(
            load,
            cw_supply,
            BLANKETED_UA_KW_PER_K,
            config,
            use_total_pressure=True,
            ncg_partial_pressure_kpa=pressures[case],
            ncg_pressure_exponent=exponent,
            ncg_reference_pressure_kpa=references[case],
        )
    if case == "ncg_blanketing_only":
        return solve_operating_point(
            load,
            cw_supply,
            BLANKETING_ONLY_UA_KW_PER_K,
            config,
            use_total_pressure=True,
            ncg_partial_pressure_kpa=0.0,
        )
    raise ValueError(f"unsupported sweep case: {case}")


def _scenario_specs(
    load: np.ndarray,
    cw_supply: np.ndarray,
    config: SimulationConfig,
    pressures: dict[str, float],
    references: dict[str, float],
) -> list[_Scenario]:
    healthy = solve_operating_point(
        load,
        cw_supply,
        config.ua_cond_kw_per_k,
        config,
    )
    scenarios = [_Scenario("healthy", 0.0, 0, healthy)]
    for case in SWEEP_CASES:
        exponents = NCG_EXPONENTS if case in NCG_CASES else (0.0,)
        for exponent_index, exponent in enumerate(exponents):
            state = _scenario_operating_point(
                case,
                exponent,
                load,
                cw_supply,
                config,
                pressures,
                references,
            )
            scenarios.append(
                _Scenario(case, exponent, exponent_index, state)
            )
    return scenarios


def _derive_noise_free_state(
    state: OperatingPoint,
    cooling_load_kw: np.ndarray,
    cw_supply_setpoint: np.ndarray,
    config: SimulationConfig,
) -> pd.DataFrame:
    plr = cooling_load_kw / config.rated_capacity_kw
    subcooling = np.clip(
        config.cond_subcooling_c
        + config.subcooling_plr_gain_c * (plr - 0.65),
        0.2,
        None,
    )
    sensors = pd.DataFrame(
        {
            "outdoor_wet_bulb_c": (
                cw_supply_setpoint - config.tower_approach_c
            ),
            "chw_supply_temp_c": np.full(
                len(cooling_load_kw), config.chw_supply_setpoint_c
            ),
            "chw_return_temp_c": state.chw_return_c,
            "cw_supply_temp_c": cw_supply_setpoint,
            "cw_return_temp_c": state.cw_return_c,
            "chw_flow_kg_s": np.full(
                len(cooling_load_kw), config.chw_flow_kg_s
            ),
            "cw_flow_kg_s": np.full(
                len(cooling_load_kw), config.cw_flow_kg_s
            ),
            "compressor_power_kw": state.compressor_power_kw,
            "suction_pressure_kpa": state.suction_pressure_kpa,
            "discharge_pressure_kpa": state.discharge_pressure_kpa,
            "cond_liquid_temp_c": state.condenser_temp_c - subcooling,
        }
    )
    return derive_signals(sensors, config)


def _expected_sweep_point_metrics(
    sweep: str,
    points: np.ndarray,
    config: SimulationConfig,
    pressures: dict[str, float],
    references: dict[str, float],
) -> pd.DataFrame:
    if sweep == "load":
        load = points
        cw_supply = np.full(len(points), REFERENCE_CW_SUPPLY_C)
    else:
        load = np.full(len(points), REFERENCE_LOAD_KW)
        cw_supply = points
    scenarios = _scenario_specs(
        load, cw_supply, config, pressures, references
    )
    healthy = _derive_noise_free_state(
        scenarios[0].operating_point, load, cw_supply, config
    )
    rows = []
    for scenario in scenarios[1:]:
        faulty = _derive_noise_free_state(
            scenario.operating_point, load, cw_supply, config
        )
        for point_index, point_value in enumerate(points):
            rows.append(
                {
                    "sweep": sweep,
                    "case": scenario.case,
                    "n": scenario.exponent,
                    "point_index": point_index,
                    "point_value": point_value,
                    "expected_approach_excess_c": (
                        faulty["condenser_approach_c"].iloc[point_index]
                        - healthy["condenser_approach_c"].iloc[point_index]
                    ),
                    "expected_apparent_subcooling_excess_c": (
                        faulty["apparent_subcooling_c"].iloc[point_index]
                        - healthy["apparent_subcooling_c"].iloc[point_index]
                    ),
                    "expected_discharge_pressure_excess_kpa": (
                        faulty["discharge_pressure_kpa"].iloc[point_index]
                        - healthy["discharge_pressure_kpa"].iloc[point_index]
                    ),
                    "expected_condenser_heat_kw": faulty[
                        "condenser_heat_kw"
                    ].iloc[point_index],
                    "expected_cw_supply_mean": faulty[
                        "cw_supply_temp_c"
                    ].iloc[point_index],
                    "ncg_partial_pressure_kpa": scenario.operating_point.ncg_partial_pressure_kpa[
                        point_index
                    ],
                }
            )
    return pd.DataFrame(rows)


def _draw_sensor_arrays(
    state: OperatingPoint,
    cooling_load_kw: np.ndarray,
    cw_supply_setpoint: np.ndarray,
    rng: np.random.Generator,
    config: SimulationConfig,
) -> dict[str, np.ndarray]:
    shape = (len(cw_supply_setpoint), SAMPLES_PER_POINT)
    temperature_sd = config.temperature_noise_sd_c
    temp_noise = lambda: rng.normal(0.0, temperature_sd, size=shape)
    outdoor_wet_bulb = (
        cw_supply_setpoint[:, None] - config.tower_approach_c + temp_noise()
    )
    chw_supply = config.chw_supply_setpoint_c + temp_noise()
    chw_return = state.chw_return_c[:, None] + temp_noise()
    cw_supply = cw_supply_setpoint[:, None] + temp_noise()
    cw_return = state.cw_return_c[:, None] + temp_noise()
    chw_flow = config.chw_flow_kg_s + rng.normal(
        0.0,
        config.chw_flow_kg_s * config.flow_noise_fraction,
        size=shape,
    )
    cw_flow = config.cw_flow_kg_s + rng.normal(
        0.0,
        config.cw_flow_kg_s * config.flow_noise_fraction,
        size=shape,
    )
    compressor_power = state.compressor_power_kw[:, None] + rng.normal(
        0.0, config.power_noise_sd_kw, size=shape
    )
    suction_pressure = state.suction_pressure_kpa[:, None] + rng.normal(
        0.0, config.pressure_noise_sd_kpa, size=shape
    )
    discharge_pressure = state.discharge_pressure_kpa[:, None] + rng.normal(
        0.0, config.pressure_noise_sd_kpa, size=shape
    )
    plr = cooling_load_kw / config.rated_capacity_kw
    subcooling = (
        config.cond_subcooling_c
        + config.subcooling_plr_gain_c * (plr[:, None] - 0.65)
        + rng.normal(0.0, config.subcooling_ar_sd_c, size=shape)
    )
    subcooling = np.clip(subcooling, 0.2, None)
    liquid_temperature = (
        state.condenser_temp_c[:, None] - subcooling + temp_noise()
    )
    cooling_load = chw_flow * config.cp_kj_per_kg_k * (
        chw_return - chw_supply
    )
    cop = cooling_load / compressor_power
    return {
        "outdoor_wet_bulb_c": outdoor_wet_bulb,
        "chw_supply_temp_c": chw_supply,
        "chw_return_temp_c": chw_return,
        "cw_supply_temp_c": cw_supply,
        "cw_return_temp_c": cw_return,
        "chw_flow_kg_s": chw_flow,
        "cw_flow_kg_s": cw_flow,
        "compressor_power_kw": compressor_power,
        "suction_pressure_kpa": suction_pressure,
        "discharge_pressure_kpa": discharge_pressure,
        "cond_liquid_temp_c": liquid_temperature,
        "cooling_load_kw": cooling_load,
        "cop": cop,
    }


def _sensor_means(
    sweep: str,
    points: np.ndarray,
    cw_supply: np.ndarray,
    config: SimulationConfig,
    scenarios: list[_Scenario],
) -> dict[str, np.ndarray]:
    shape = (
        len(scenarios),
        len(SWEEP_SEEDS),
        len(points),
        SAMPLES_PER_POINT,
    )
    sensor_fields = (
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
        "cond_liquid_temp_c",
        "cooling_load_kw",
        "cop",
    )
    arrays = {
        name: np.empty(shape, dtype=float)
        for name in sensor_fields
    }
    for scenario_index, scenario in enumerate(scenarios):
        for seed_index, seed in enumerate(SWEEP_SEEDS):
            if scenario.case == "healthy":
                rng = np.random.default_rng([seed, 0 if sweep == "load" else 1, 0])
            else:
                rng = np.random.default_rng(
                    [
                        seed,
                        0 if sweep == "load" else 1,
                        CASE_INDEX[scenario.case] + 1,
                        scenario.exponent_index,
                    ]
                )
            drawn = _draw_sensor_arrays(
                scenario.operating_point,
                (
                    points
                    if sweep == "load"
                    else np.full(len(points), REFERENCE_LOAD_KW)
                ),
                cw_supply,
                rng,
                config,
            )
            for name in sensor_fields:
                arrays[name][scenario_index, seed_index] = drawn[name]

    row_count = int(np.prod(shape))
    sensors = pd.DataFrame(
        {
            name: values.reshape(row_count)
            for name, values in arrays.items()
        }
    )
    timestamps = pd.date_range(
        "2025-01-01", periods=len(points) * SAMPLES_PER_POINT, freq="min"
    )
    sensors.insert(
        0,
        "timestamp",
        np.tile(timestamps, len(scenarios) * len(SWEEP_SEEDS)),
    )
    derived = derive_signals(sensors, config)
    derived_fields = (
        "condenser_approach_c",
        "apparent_subcooling_c",
        "discharge_pressure_kpa",
        "condenser_heat_kw",
        "cw_supply_temp_c",
    )
    return {
        name: derived[name]
        .to_numpy()
        .reshape(shape)
        .mean(axis=-1)
        for name in derived_fields
    }


def _ols_slope(x: np.ndarray, y: np.ndarray) -> float:
    design = np.column_stack([np.ones(len(x)), x])
    coefficients, *_ = np.linalg.lstsq(design, y, rcond=None)
    return float(coefficients[1])


def _sweep_point_metrics(
    sweep: str,
    points: np.ndarray,
    config: SimulationConfig,
    pressures: dict[str, float],
    references: dict[str, float],
) -> pd.DataFrame:
    if sweep == "load":
        load = points
        cw_supply = np.full(len(points), REFERENCE_CW_SUPPLY_C)
    else:
        load = np.full(len(points), REFERENCE_LOAD_KW)
        cw_supply = points
    scenarios = _scenario_specs(
        load, cw_supply, config, pressures, references
    )
    means = _sensor_means(
        sweep, points, cw_supply, config, scenarios
    )
    rows = []
    for scenario_index, scenario in enumerate(scenarios[1:], start=1):
        for seed_index, seed in enumerate(SWEEP_SEEDS):
            for point_index, point_value in enumerate(points):
                rows.append(
                    {
                        "sweep": sweep,
                        "case": scenario.case,
                        "n": scenario.exponent,
                        "seed": seed,
                        "point_index": point_index,
                        "point_value": point_value,
                        "load_kw": load[point_index],
                        "cw_supply_setpoint_c": cw_supply[point_index],
                        "approach_excess_c": (
                            means["condenser_approach_c"][
                                scenario_index, seed_index, point_index
                            ]
                            - means["condenser_approach_c"][
                                0, seed_index, point_index
                            ]
                        ),
                        "apparent_subcooling_excess_c": (
                            means["apparent_subcooling_c"][
                                scenario_index, seed_index, point_index
                            ]
                            - means["apparent_subcooling_c"][
                                0, seed_index, point_index
                            ]
                        ),
                        "discharge_pressure_excess_kpa": (
                            means["discharge_pressure_kpa"][
                                scenario_index, seed_index, point_index
                            ]
                            - means["discharge_pressure_kpa"][
                                0, seed_index, point_index
                            ]
                        ),
                        "condenser_heat_kw": means["condenser_heat_kw"][
                            scenario_index, seed_index, point_index
                        ],
                        "cw_supply_mean": means["cw_supply_temp_c"][
                            scenario_index, seed_index, point_index
                        ],
                    }
                )
    return pd.DataFrame(rows)


def _signature_distributions(point_metrics: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (sweep, case, exponent, seed), group in point_metrics.groupby(
        ["sweep", "case", "n", "seed"], sort=False
    ):
        if sweep == "load":
            excess = group["approach_excess_c"].to_numpy(dtype=float)
            heat = group["condenser_heat_kw"].to_numpy(dtype=float)
            if np.isfinite(excess).all() and np.isfinite(heat).all() and np.all(
                excess > 0.0
            ):
                value = _ols_slope(np.log(heat), np.log(excess))
            else:
                value = np.nan
            rows.append(
                {
                    "signature": LOAD_ELASTICITY,
                    "sweep": sweep,
                    "case": case,
                    "n": exponent,
                    "seed": seed,
                    "value": value,
                }
            )
        else:
            excess = group["approach_excess_c"].to_numpy(dtype=float)
            supply = group["cw_supply_mean"].to_numpy(dtype=float)
            if np.isfinite(excess).all() and np.isfinite(supply).all():
                centered_supply = supply - REFERENCE_CW_SUPPLY_C
                design = np.column_stack(
                    [np.ones(len(centered_supply)), centered_supply]
                )
                coefficients, *_ = np.linalg.lstsq(
                    design, excess, rcond=None
                )
                value = (
                    float(100.0 * coefficients[1] / coefficients[0])
                    if coefficients[0] != 0.0
                    else np.nan
                )
            else:
                value = np.nan
            rows.append(
                {
                    "signature": CW_SENSITIVITY,
                    "sweep": sweep,
                    "case": case,
                    "n": exponent,
                    "seed": seed,
                    "value": value,
                }
            )
    return pd.DataFrame(rows)


def _expected_signature_distributions(
    expected_point_metrics: pd.DataFrame,
) -> pd.DataFrame:
    rows = []
    for (sweep, case, exponent), group in expected_point_metrics.groupby(
        ["sweep", "case", "n"], sort=False
    ):
        approach = group["expected_approach_excess_c"].to_numpy(dtype=float)
        if sweep == "load":
            heat = group["expected_condenser_heat_kw"].to_numpy(dtype=float)
            if np.isfinite(approach).all() and np.isfinite(heat).all() and np.all(
                approach > 0.0
            ):
                value = _ols_slope(np.log(heat), np.log(approach))
            else:
                value = np.nan
            signature = LOAD_ELASTICITY
        else:
            supply = group["expected_cw_supply_mean"].to_numpy(dtype=float)
            if np.isfinite(approach).all() and np.isfinite(supply).all():
                centered_supply = supply - REFERENCE_CW_SUPPLY_C
                design = np.column_stack(
                    [np.ones(len(centered_supply)), centered_supply]
                )
                coefficients, *_ = np.linalg.lstsq(
                    design, approach, rcond=None
                )
                value = (
                    float(100.0 * coefficients[1] / coefficients[0])
                    if coefficients[0] != 0.0
                    else np.nan
                )
            else:
                value = np.nan
            signature = CW_SENSITIVITY
        rows.append(
            {
                "signature": signature,
                "sweep": sweep,
                "case": case,
                "n": exponent,
                "value": value,
            }
        )
    return pd.DataFrame(rows)


def _separation_rows(
    point_metrics: pd.DataFrame, signatures: pd.DataFrame
) -> pd.DataFrame:
    rows = []
    cases_and_exponents = []
    for case in NCG_CASES:
        cases_and_exponents.extend(
            (case, exponent) for exponent in NCG_EXPONENTS
        )
    cases_and_exponents.append(("ncg_blanketing_only", 0.0))

    for signature, (applies_to, _) in SIGNATURE_SPECS.items():
        sweeps = ("load", "cw") if applies_to == "both" else (applies_to,)
        for sweep in sweeps:
            for case, exponent in cases_and_exponents:
                if signature == SUBCOOLING_EXCESS:
                    case_values = point_metrics.loc[
                        (point_metrics["sweep"] == sweep)
                        & (point_metrics["case"] == case)
                        & (point_metrics["n"] == exponent)
                    ]
                    reference_values = point_metrics.loc[
                        (point_metrics["sweep"] == sweep)
                        & (point_metrics["case"] == "condenser_fouling")
                    ]
                    point_indexes = sorted(
                        case_values["point_index"].unique().tolist()
                    )
                else:
                    case_values = signatures.loc[
                        (signatures["sweep"] == sweep)
                        & (signatures["signature"] == signature)
                        & (signatures["case"] == case)
                        & (signatures["n"] == exponent),
                        "value",
                    ].to_numpy(dtype=float)
                    reference_values = signatures.loc[
                        (signatures["sweep"] == sweep)
                        & (signatures["signature"] == signature)
                        & (signatures["case"] == "condenser_fouling"),
                        "value",
                    ].to_numpy(dtype=float)
                    point_indexes = [None]

                for point_index in point_indexes:
                    if signature == SUBCOOLING_EXCESS:
                        case_group = case_values.loc[
                            case_values["point_index"] == point_index
                        ]
                        reference_group = reference_values.loc[
                            reference_values["point_index"] == point_index
                        ]
                        case_distribution = case_group[
                            "apparent_subcooling_excess_c"
                        ].to_numpy(dtype=float)
                        reference_distribution = reference_group[
                            "apparent_subcooling_excess_c"
                        ].to_numpy(dtype=float)
                        point_value = float(case_group["point_value"].iloc[0])
                    else:
                        case_distribution = case_values
                        reference_distribution = reference_values
                        point_value = np.nan

                    finite = (
                        len(case_distribution) > 0
                        and len(reference_distribution) > 0
                        and np.isfinite(case_distribution).all()
                        and np.isfinite(reference_distribution).all()
                    )
                    if finite:
                        ref_min = float(reference_distribution.min())
                        ref_max = float(reference_distribution.max())
                        case_min = float(case_distribution.min())
                        case_max = float(case_distribution.max())
                        gap = interval_gap(
                            reference_distribution, case_distribution
                        )
                        direction = float(
                            np.sign(
                                np.median(case_distribution)
                                - np.median(reference_distribution)
                            )
                        )
                    else:
                        ref_min = ref_max = case_min = case_max = gap = direction = np.nan
                    rows.append(
                        {
                            "signature": signature,
                            "sweep": sweep,
                            "case": case,
                            "n": exponent,
                            "point_index": point_index,
                            "point_value": point_value,
                            "ref_min": ref_min,
                            "ref_max": ref_max,
                            "case_min": case_min,
                            "case_max": case_max,
                            "gap": gap,
                            "separable": bool(finite and gap > 0.0),
                            "direction": direction,
                        }
                    )
    return pd.DataFrame(rows)


def _add_expected_values(
    separation: pd.DataFrame,
    expected_point_metrics: pd.DataFrame,
    expected_signatures: pd.DataFrame,
) -> pd.DataFrame:
    expected_points = {
        (row.sweep, row.case, float(row.n), int(row.point_index)): row
        for row in expected_point_metrics.itertuples(index=False)
    }
    expected_signature_values = {
        (row.signature, row.sweep, row.case, float(row.n)): float(row.value)
        for row in expected_signatures.itertuples(index=False)
    }
    rows = []
    for row in separation.itertuples(index=False):
        if row.signature == SUBCOOLING_EXCESS:
            point_key = (
                row.sweep,
                row.case,
                float(row.n),
                int(row.point_index),
            )
            reference_key = (
                row.sweep,
                "condenser_fouling",
                0.0,
                int(row.point_index),
            )
            case_expected = expected_points[point_key]
            reference_expected = expected_points[reference_key]
            expected_value = float(
                case_expected.expected_apparent_subcooling_excess_c
            )
            expected_ref = float(
                reference_expected.expected_apparent_subcooling_excess_c
            )
            ncg_pressure = float(
                case_expected.ncg_partial_pressure_kpa
            )
        else:
            expected_value = expected_signature_values[
                (
                    row.signature,
                    row.sweep,
                    row.case,
                    float(row.n),
                )
            ]
            expected_ref = expected_signature_values[
                (
                    row.signature,
                    row.sweep,
                    "condenser_fouling",
                    0.0,
                )
            ]
            ncg_pressure = np.nan
        if abs(expected_value) < 1e-12:
            expected_value = 0.0
        if abs(expected_ref) < 1e-12:
            expected_ref = 0.0
        rows.append(
            {
                "expected_value": expected_value,
                "expected_ref": expected_ref,
                "expected_direction": (
                    float(np.sign(expected_value - expected_ref))
                    if np.isfinite(expected_value) and np.isfinite(expected_ref)
                    else np.nan
                ),
                "ncg_partial_pressure_kpa": ncg_pressure,
            }
        )
    expected = pd.DataFrame(rows, index=separation.index)
    return pd.concat([separation, expected], axis=1)


def collapse_bands(separation: pd.DataFrame) -> dict[tuple[str, str, str], tuple[float, ...]]:
    bands = {}
    keys = separation[["signature", "sweep", "case"]].drop_duplicates()
    for signature, sweep, case in keys.itertuples(index=False, name=None):
        rows = separation.loc[
            (separation["signature"] == signature)
            & (separation["sweep"] == sweep)
            & (separation["case"] == case)
        ]
        n0 = rows.loc[rows["n"] == 0.0]
        n0_directions = {
            ("aggregate" if pd.isna(point) else point): direction
            for point, direction in zip(
                n0["point_index"].tolist(), n0["direction"].tolist()
            )
        }
        collapsed = []
        for exponent, exponent_rows in rows.groupby("n", sort=True):
            direction_changed = False
            for point, direction in zip(
                exponent_rows["point_index"].tolist(),
                exponent_rows["direction"].tolist(),
            ):
                point_key = "aggregate" if pd.isna(point) else point
                if (
                    point_key in n0_directions
                    and direction != n0_directions[point_key]
                ):
                    direction_changed = True
            if (
                not exponent_rows["separable"].astype(bool).all()
                or direction_changed
            ):
                collapsed.append(float(exponent))
        bands[(signature, sweep, case)] = tuple(collapsed)
    return bands


def collapse_kind(
    separation: pd.DataFrame,
    signature: str,
    sweep: str,
    case: str,
) -> str:
    if case == "ncg_blanketing_only":
        return "identical to fouling by construction"

    band = collapse_bands(separation).get((signature, sweep, case), ())
    if not band:
        return "none"

    rows = separation.loc[
        (separation["signature"] == signature)
        & (separation["sweep"] == sweep)
        & (separation["case"] == case)
    ]
    reference_directions = {
        ("aggregate" if pd.isna(point) else point): direction
        for point, direction in zip(
            rows.loc[rows["n"] == 0.0, "point_index"].tolist(),
            rows.loc[rows["n"] == 0.0, "expected_direction"].tolist(),
        )
    }
    for exponent in band:
        exponent_rows = rows.loc[rows["n"] == exponent]
        for point, direction in zip(
            exponent_rows["point_index"].tolist(),
            exponent_rows["expected_direction"].tolist(),
        ):
            point_key = "aggregate" if pd.isna(point) else point
            reference_direction = reference_directions.get(point_key, np.nan)
            if (
                not np.isfinite(direction)
                or direction == 0.0
                or not np.isfinite(reference_direction)
                or direction != reference_direction
            ):
                return "expected-value crossing"
    return "finite-sample overlap only"


def separation_claims(separation: pd.DataFrame) -> pd.DataFrame:
    bands = collapse_bands(separation)
    rows = []
    for signature, (_, source) in SIGNATURE_SPECS.items():
        sweeps = ("load", "cw") if SIGNATURE_SPECS[signature][0] == "both" else (
            SIGNATURE_SPECS[signature][0],
        )
        claimed = True
        for case in NCG_CASES:
            for sweep in sweeps:
                relevant = separation.loc[
                    (separation["signature"] == signature)
                    & (separation["sweep"] == sweep)
                    & (separation["case"] == case)
                ]
                expected_n = set(NCG_EXPONENTS)
                present_n = set(relevant["n"].astype(float).unique())
                if present_n != expected_n:
                    claimed = False
                    continue
                if bands.get((signature, sweep, case), ()):
                    claimed = False
        rows.append(
            {
                "signature": signature,
                "source": source,
                "claimed": bool(claimed),
            }
        )
    return pd.DataFrame(
        rows, columns=["signature", "source", "claimed"]
    )


def _fmt_exponents(values: tuple[float, ...]) -> str:
    return ", ".join(f"{value:.1f}" for value in values) if values else "none"


def _expected_value_separators(results: SweepResults) -> list[str]:
    unclaimed = results.claims.loc[
        ~results.claims["claimed"], "signature"
    ].tolist()
    separators = []
    for signature in unclaimed:
        applies_to = SIGNATURE_SPECS[signature][0]
        sweeps = ("load", "cw") if applies_to == "both" else (applies_to,)
        kinds = []
        positive_everywhere = True
        for case in NCG_CASES:
            expected_points = results.expected_point_metrics.loc[
                (results.expected_point_metrics["case"] == case)
                & results.expected_point_metrics["n"].isin(NCG_EXPONENTS)
            ]
            if not expected_points["ncg_partial_pressure_kpa"].gt(0.0).all():
                positive_everywhere = False
            for sweep in sweeps:
                rows = results.separation.loc[
                    (results.separation["signature"] == signature)
                    & (results.separation["sweep"] == sweep)
                    & (results.separation["case"] == case)
                ]
                if rows.empty or not rows["expected_value"].gt(0.0).all():
                    positive_everywhere = False
                kinds.append(collapse_kind(results.separation, signature, sweep, case))
        if (
            positive_everywhere
            and "finite-sample overlap only" in kinds
            and all(kind in {"none", "finite-sample overlap only"} for kind in kinds)
        ):
            separators.append(signature)
    return separators


def _minimum_expected_subcooling_lines(results: SweepResults) -> list[str]:
    lines = []
    for case in NCG_CASES:
        rows = results.separation.loc[
            (results.separation["signature"] == SUBCOOLING_EXCESS)
            & (results.separation["case"] == case)
        ]
        row = rows.loc[rows["expected_value"].idxmin()]
        if row["sweep"] == "load":
            location = f"{row['point_value']:.0f} kW"
        else:
            location = f"{row['point_value']:.0f} °C CW inlet"
        lines.append(
            f"Minimum expected apparent-subcooling excess, {case}: "
            f"{row['expected_value']:.3f} K at {location}, n={row['n']:.1f}; "
            f"p_ncg={row['ncg_partial_pressure_kpa']:.3f} kPa."
        )
    overlap = results.separation.loc[
        (results.separation["signature"] == SUBCOOLING_EXCESS)
        & (results.separation["sweep"] == "cw")
        & (results.separation["case"] == "ncg_blanketed")
        & (results.separation["point_value"] == CW_POINTS_C[0])
        & ~results.separation["separable"].astype(bool)
    ]
    if not overlap.empty:
        exponents = sorted(overlap["n"].unique())
        ref_min = float(overlap["ref_min"].min())
        ref_max = float(overlap["ref_max"].max())
        expected_min = float(overlap["expected_value"].min())
        expected_max = float(overlap["expected_value"].max())
        pressure_min = float(overlap["ncg_partial_pressure_kpa"].min())
        pressure_max = float(overlap["ncg_partial_pressure_kpa"].max())
        lines.append(
            "Blanketed CW finite-sample overlap at "
            f"{CW_POINTS_C[0]:.0f} °C, n={_fmt_exponents(tuple(exponents))}: "
            f"expected excess {expected_min:.3f}–{expected_max:.3f} K; "
            f"p_ncg={pressure_min:.3f}–{pressure_max:.3f} kPa; "
            f"fouling 20-seed range {ref_min:.3f}–{ref_max:.3f} K."
        )
    return lines


def sweep_markdown(results: SweepResults) -> str:
    calibration_lines = [
        "| case | condenser UA (kW/K) | p_ncg at reference (kPa) | "
        "reference P_sat (kPa) |",
        "| --- | ---: | ---: | ---: |",
    ]
    for row in results.calibration.itertuples(index=False):
        calibration_lines.append(
            f"| {row.case} | {row.condenser_ua_kw_per_k:.1f} | "
            f"{row.ncg_partial_pressure_kpa:.3f} | "
            f"{row.ncg_reference_pressure_kpa:.3f} |"
        )

    bands = collapse_bands(results.separation)
    separation_lines = [
        "| signature | sweep | case | separable at n=0 | collapse band n | collapse kind |",
        "| --- | --- | --- |:---:| --- | --- |",
    ]
    keys = (
        results.separation[
            ["signature", "sweep", "case"]
        ]
        .drop_duplicates()
        .sort_values(["signature", "sweep", "case"])
    )
    for signature, sweep, case in keys.itertuples(index=False, name=None):
        n0 = results.separation.loc[
            (results.separation["signature"] == signature)
            & (results.separation["sweep"] == sweep)
            & (results.separation["case"] == case)
            & (results.separation["n"] == 0.0)
        ]
        n0_separable = bool(n0["separable"].astype(bool).all())
        separation_lines.append(
            f"| {signature} | {sweep} | {case} | "
            f"{'yes' if n0_separable else 'no'} | "
            f"{_fmt_exponents(bands[(signature, sweep, case)])} | "
            f"{collapse_kind(results.separation, signature, sweep, case)} |"
        )

    claimed = results.claims.loc[
        results.claims["claimed"], "signature"
    ].tolist()
    not_claimed = results.claims.loc[
        ~results.claims["claimed"], "signature"
    ].tolist()
    load_nan = int(
        results.signatures.loc[
            (results.signatures["signature"] == LOAD_ELASTICITY)
            & results.signatures["value"].isna()
        ].shape[0]
    )
    expected_value_separators = _expected_value_separators(results)
    return "\n".join(
        [
            "#### Calibration at the reference operating point",
            "",
            *calibration_lines,
            "",
            "#### Separation and collapse bands",
            "",
            *separation_lines,
            "",
            f"Load-elasticity NaN rows (nonpositive approach excess): {load_nan}.",
            "",
            "Claimed separators (every sweep, every n, every NCG case with nonzero partial pressure): "
            + (", ".join(claimed) if claimed else "none")
            + ".",
            "",
            "Not claimed (collapses or reverses in at least one sweep): "
            + (", ".join(not_claimed) if not_claimed else "none")
            + ".",
            "",
            "Expected-value (noise-free) separators: "
            + (
                ", ".join(expected_value_separators)
                if expected_value_separators
                else "none"
            )
            + ".",
            "",
            *_minimum_expected_subcooling_lines(results),
            "",
            "ncg_blanketing_only (fouling by construction): no separator claimed.",
        ]
    )


def _replace_generated_block(report_path: Path, block: str) -> None:
    report = report_path.read_text()
    if BEGIN_MARKER not in report or END_MARKER not in report:
        raise ValueError("sweep-separation generated markers are missing from report")
    before, rest = report.split(BEGIN_MARKER, maxsplit=1)
    _, after = rest.split(END_MARKER, maxsplit=1)
    report_path.write_text(
        f"{before}{BEGIN_MARKER}\n{block}\n{END_MARKER}{after}"
    )


def run_sweeps(config: SimulationConfig = SimulationConfig()) -> SweepResults:
    pressures, references = _calibrations(config)
    calibration = _calibration_table(config, pressures, references)
    load_points = _sweep_point_metrics(
        "load",
        LOAD_POINTS_KW,
        config,
        pressures,
        references,
    )
    cw_points = _sweep_point_metrics(
        "cw",
        CW_POINTS_C,
        config,
        pressures,
        references,
    )
    point_metrics = pd.concat([load_points, cw_points], ignore_index=True)
    signatures = _signature_distributions(point_metrics)
    expected_load_points = _expected_sweep_point_metrics(
        "load",
        LOAD_POINTS_KW,
        config,
        pressures,
        references,
    )
    expected_cw_points = _expected_sweep_point_metrics(
        "cw",
        CW_POINTS_C,
        config,
        pressures,
        references,
    )
    expected_point_metrics = pd.concat(
        [expected_load_points, expected_cw_points], ignore_index=True
    )
    expected_signatures = _expected_signature_distributions(
        expected_point_metrics
    )
    separation = _add_expected_values(
        _separation_rows(point_metrics, signatures),
        expected_point_metrics,
        expected_signatures,
    )
    claims = separation_claims(separation)
    return SweepResults(
        point_metrics=point_metrics,
        signatures=signatures,
        expected_point_metrics=expected_point_metrics,
        expected_signatures=expected_signatures,
        separation=separation,
        claims=claims,
        calibration=calibration,
    )


def _plot_n0_sweep(point_metrics: pd.DataFrame, output_dir: Path) -> None:
    cases = SWEEP_CASES
    colors = {
        "condenser_fouling": "#252525",
        "ncg_dalton": "#0072B2",
        "ncg_blanketed": "#D55E00",
        "ncg_blanketing_only": "#009E73",
    }
    names = {
        "condenser_fouling": "Condenser fouling",
        "ncg_dalton": "Dalton NCG",
        "ncg_blanketed": "Blanketed NCG",
        "ncg_blanketing_only": "Blanketing-only NCG",
    }
    for sweep, filename in (
        ("load", "sweep_load_signature.png"),
        ("cw", "sweep_cw_inlet_signature.png"),
    ):
        subset = point_metrics.loc[
            (point_metrics["sweep"] == sweep) & (point_metrics["n"] == 0.0)
        ]
        figure, axes = plt.subplots(1, 3, figsize=(16, 4.6))
        for case in cases:
            data = subset.loc[subset["case"] == case]
            means = (
                data.groupby(["point_index", "point_value"], as_index=False)
                .agg(
                    approach_excess_c=("approach_excess_c", "mean"),
                    apparent_subcooling_excess_c=(
                        "apparent_subcooling_excess_c",
                        "mean",
                    ),
                    discharge_pressure_excess_kpa=(
                        "discharge_pressure_excess_kpa",
                        "mean",
                    ),
                    condenser_heat_kw=("condenser_heat_kw", "mean"),
                )
                .sort_values("point_index")
            )
            x = (
                means["condenser_heat_kw"]
                if sweep == "load"
                else means["point_value"]
            )
            axes[0].plot(
                x,
                means["approach_excess_c"],
                marker="o",
                color=colors[case],
                label=names[case],
            )
            axes[1].plot(
                means["point_value"],
                means["apparent_subcooling_excess_c"],
                marker="o",
                color=colors[case],
                label=names[case],
            )
            axes[2].plot(
                means["point_value"],
                means["discharge_pressure_excess_kpa"],
                marker="o",
                color=colors[case],
                label=names[case],
            )
            reference_point = (
                REFERENCE_LOAD_KW
                if sweep == "load"
                else REFERENCE_CW_SUPPLY_C
            )
            reference_row = means.loc[
                np.isclose(means["point_value"], reference_point)
            ]
            if not reference_row.empty:
                row = reference_row.iloc[0]
                axes[0].scatter(
                    [row["condenser_heat_kw"] if sweep == "load" else reference_point],
                    [row["approach_excess_c"]],
                    marker="*",
                    s=150,
                    color=colors[case],
                    edgecolor="white",
                    zorder=4,
                )
                axes[1].scatter(
                    [reference_point],
                    [row["apparent_subcooling_excess_c"]],
                    marker="*",
                    s=150,
                    color=colors[case],
                    edgecolor="white",
                    zorder=4,
                )
                axes[2].scatter(
                    [reference_point],
                    [row["discharge_pressure_excess_kpa"]],
                    marker="*",
                    s=150,
                    color=colors[case],
                    edgecolor="white",
                    zorder=4,
                )
        if sweep == "load":
            axes[0].set_xscale("log")
            axes[0].set_yscale("log")
            axes[0].set_xlabel("Faulty mean condenser heat (kW)")
            axes[1].set_xlabel("Cooling load (kW)")
            axes[2].set_xlabel("Cooling load (kW)")
            figure.suptitle("Load sweep at n=0 (seed-averaged)")
        else:
            axes[0].set_xlabel("CW inlet temperature (°C)")
            axes[1].set_xlabel("CW inlet temperature (°C)")
            axes[2].set_xlabel("CW inlet temperature (°C)")
            figure.suptitle("CW-inlet sweep at n=0 (seed-averaged)")
        axes[0].set_ylabel("Approach excess (°C)")
        axes[1].set_ylabel("Apparent-subcooling excess (°C)")
        axes[2].set_ylabel("Discharge-pressure excess (kPa)")
        axes[0].set_title(
            "Approach excess vs condenser heat" if sweep == "load"
            else "Approach excess vs CW inlet"
        )
        axes[1].set_title("Apparent subcooling")
        axes[2].set_title("Discharge pressure")
        axes[0].legend(fontsize=8)
        figure.tight_layout()
        figure.savefig(output_dir / filename, dpi=160)
        plt.close(figure)


def _plot_exponent_sweep(results: SweepResults, output_dir: Path) -> None:
    figure, axes = plt.subplots(1, 3, figsize=(17, 5))
    panels = (
        (LOAD_ELASTICITY, "load", "Approach load elasticity"),
        (CW_SENSITIVITY, "cw", "CW sensitivity (%/K)"),
        (SUBCOOLING_EXCESS, "both", "Minimum subcooling excess (°C)"),
    )
    colors = {"ncg_dalton": "#0072B2", "ncg_blanketed": "#D55E00"}
    bands = collapse_bands(results.separation)
    for axis, (signature, sweep, title) in zip(axes, panels):
        if signature == SUBCOOLING_EXCESS:
            sweep_points = results.point_metrics
            values = (
                sweep_points.groupby(["case", "n", "seed"])[
                    "apparent_subcooling_excess_c"
                ]
                .min()
                .reset_index(name="value")
            )
            fouling = values.loc[values["case"] == "condenser_fouling", "value"]
            n_values = NCG_EXPONENTS
            applicable_sweep = "load"
        else:
            values = results.signatures.loc[
                results.signatures["signature"] == signature
            ]
            fouling = values.loc[values["case"] == "condenser_fouling", "value"]
            n_values = NCG_EXPONENTS
            applicable_sweep = sweep
        if len(fouling):
            axis.axhspan(
                float(fouling.min()),
                float(fouling.max()),
                color="#999999",
                alpha=0.22,
                label="Fouling min–max",
            )
        for case in NCG_CASES:
            series = values.loc[values["case"] == case]
            medians = []
            minima = []
            maxima = []
            for exponent in n_values:
                distribution = series.loc[
                    series["n"] == exponent, "value"
                ].to_numpy(dtype=float)
                finite_distribution = distribution[np.isfinite(distribution)]
                if len(finite_distribution):
                    medians.append(float(np.median(finite_distribution)))
                    minima.append(float(finite_distribution.min()))
                    maxima.append(float(finite_distribution.max()))
                else:
                    medians.append(np.nan)
                    minima.append(np.nan)
                    maxima.append(np.nan)
            axis.fill_between(
                n_values,
                minima,
                maxima,
                color=colors[case],
                alpha=0.15,
            )
            axis.plot(
                n_values,
                medians,
                marker="o",
                color=colors[case],
                label=case,
            )
            collapse_sweeps = (
                ("load", "cw")
                if signature == SUBCOOLING_EXCESS
                else (applicable_sweep,)
            )
            collapse = tuple(
                sorted(
                    {
                        exponent
                        for collapse_sweep in collapse_sweeps
                        for exponent in bands.get(
                            (signature, collapse_sweep, case), ()
                        )
                    }
                )
            )
            for exponent in collapse:
                axis.axvline(
                    exponent,
                    color=colors[case],
                    linestyle=":",
                    alpha=0.32,
                    linewidth=1.2,
                )
        axis.set_title(
            "Minimum subcooling excess over both sweeps"
            if signature == SUBCOOLING_EXCESS
            else title
        )
        axis.set_xlabel("NCG pressure exponent n")
        axis.grid(alpha=0.2)
        axis.legend(fontsize=8)
    figure.suptitle(
        "NCG exponent signatures: median/min–max over seeds; dotted lines mark collapse"
    )
    figure.tight_layout()
    figure.savefig(output_dir / "sweep_ncg_exponent.png", dpi=160)
    plt.close(figure)


def generate_sweep_report(
    output_dir: str | Path = "reports",
    report_path: str | Path = "diagnostic_report.md",
) -> SweepResults:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    results = run_sweeps()
    results.separation.to_csv(
        output_dir / "sweep_separation_summary.csv", index=False
    )
    _plot_n0_sweep(results.point_metrics, output_dir)
    _plot_exponent_sweep(results, output_dir)
    _replace_generated_block(Path(report_path), sweep_markdown(results))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate steady-state condenser operating-point sweeps."
    )
    parser.add_argument("--output-dir", default="reports")
    parser.add_argument("--report", default="diagnostic_report.md")
    args = parser.parse_args()
    generate_sweep_report(args.output_dir, args.report)


if __name__ == "__main__":
    main()
