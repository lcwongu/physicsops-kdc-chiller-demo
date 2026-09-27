import argparse
from dataclasses import dataclass, replace
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from chiller_sim.model import (
    SimulationConfig,
    _ar1,
    generate_telemetry,
    saturation_temperature_c,
    solve_operating_point,
)
from chiller_sim.separation import FAULT_CASES
from chiller_sim import sweeps


DETECTOR_INPUTS = (
    "timestamp",
    "discharge_pressure_kpa",
    "cond_liquid_temp_c",
    "cooling_load_kw",
)
COMMISSIONING_HOURS = 48
WINDOW_HOURS = 24
EVAL_EVERY_HOURS = 1
THRESHOLD_Z = 4.0
CALIBRATION_SEEDS = tuple(range(20))
HOLDOUT_SEEDS = tuple(range(20, 40))
DETECTOR_SEEDS = CALIBRATION_SEEDS + HOLDOUT_SEEDS
WINDOW_SAMPLES = WINDOW_HOURS * 60 // 5

TIMESERIES_CASES = (
    "healthy",
    "condenser_fouling",
    "ncg_blanketing_only",
    "ncg_dalton",
    "ncg_blanketed",
    "ncg_dalton_ua_matched",
    "ncg_blanketed_ua_matched",
)
NONZERO_NCG_CASES = TIMESERIES_CASES[3:]
FALSE_ALARM_CASES = (
    "healthy",
    "condenser_fouling",
    "ncg_blanketing_only",
)
SWEEP_CASES = (
    "healthy",
    "condenser_fouling",
    "ncg_blanketing_only",
    "ncg_dalton",
    "ncg_blanketed",
)
SWEEP_CASE_INDEX = {case: index for index, case in enumerate(SWEEP_CASES)}
PLOT_TIMESERIES_CASES = (
    "healthy",
    "condenser_fouling",
    "ncg_blanketing_only",
    "ncg_dalton",
    "ncg_blanketed",
)
BEGIN_MARKER = "<!-- BEGIN GENERATED: ncg-detector -->"
END_MARKER = "<!-- END GENERATED: ncg-detector -->"
SECTION_HEADING = (
    "## Fourth analysis: apparent-subcooling trend-and-threshold detector"
)


@dataclass(frozen=True)
class DetectorResults:
    threshold: dict[str, float]
    calibration_populations: dict[str, np.ndarray]
    calibration_values: np.ndarray
    timeseries_summary: pd.DataFrame
    timeseries_statistics: pd.DataFrame
    sweep_detection: pd.DataFrame
    sweep_windows: pd.DataFrame
    false_alarms: pd.DataFrame
    delay_summary: pd.DataFrame


def _detector_frame(frame: pd.DataFrame) -> pd.DataFrame:
    missing = set(DETECTOR_INPUTS) - set(frame.columns)
    if missing:
        raise ValueError(f"detector frame is missing required inputs: {sorted(missing)}")
    sensors = frame.loc[:, DETECTOR_INPUTS].copy()
    sensors["timestamp"] = pd.to_datetime(sensors["timestamp"])
    if not sensors["timestamp"].is_monotonic_increasing:
        raise ValueError("detector timestamps must be increasing")
    return sensors


def apparent_subcooling(frame: pd.DataFrame) -> np.ndarray:
    sensors = _detector_frame(frame)
    return np.asarray(
        saturation_temperature_c(sensors["discharge_pressure_kpa"].to_numpy())
        - sensors["cond_liquid_temp_c"].to_numpy(),
        dtype=float,
    )


def fit_baseline(
    frame: pd.DataFrame,
    config: SimulationConfig = SimulationConfig(),
) -> tuple[float, float]:
    sensors = _detector_frame(frame)
    timestamps = sensors["timestamp"]
    commissioning_end = timestamps.iloc[0] + pd.Timedelta(
        hours=COMMISSIONING_HOURS
    )
    commissioning = timestamps < commissioning_end
    plr = (
        sensors.loc[commissioning, "cooling_load_kw"].to_numpy(dtype=float)
        / config.rated_capacity_kw
    )
    subcooling = apparent_subcooling(sensors.loc[commissioning])
    if len(plr) < 2:
        raise ValueError("commissioning frame must contain at least two samples")
    design = np.column_stack([np.ones(len(plr)), plr])
    coefficients, *_ = np.linalg.lstsq(design, subcooling, rcond=None)
    return float(coefficients[0]), float(coefficients[1])


def detector_statistic(
    frame: pd.DataFrame,
    baseline: tuple[float, float],
    config: SimulationConfig = SimulationConfig(),
) -> pd.Series:
    sensors = _detector_frame(frame)
    timestamps = sensors["timestamp"]
    elapsed_seconds = (
        (timestamps - timestamps.iloc[0]).dt.total_seconds().to_numpy()
    )
    plr = (
        sensors["cooling_load_kw"].to_numpy(dtype=float)
        / config.rated_capacity_kw
    )
    a, b = baseline
    residual = apparent_subcooling(sensors) - (a + b * plr)
    evaluation_mask = (
        (elapsed_seconds >= (COMMISSIONING_HOURS + WINDOW_HOURS) * 3600)
        & (elapsed_seconds % (EVAL_EVERY_HOURS * 3600) == 0)
    )
    evaluation_indices = np.flatnonzero(evaluation_mask)
    prefix = np.concatenate([[0.0], np.cumsum(residual, dtype=float)])
    values = []
    for index in evaluation_indices:
        window_start = elapsed_seconds[index] - WINDOW_HOURS * 3600
        left = int(np.searchsorted(elapsed_seconds, window_start, side="right"))
        right = int(index + 1)
        count = right - left
        values.append((prefix[right] - prefix[left]) / count)
    return pd.Series(
        values,
        index=pd.DatetimeIndex(timestamps.iloc[evaluation_indices]),
        name="statistic",
        dtype=float,
    )


def _case_config(
    case: str, seed: int, config: SimulationConfig
) -> SimulationConfig:
    if case == "healthy":
        return replace(
            config,
            seed=seed,
            fault_type="condenser_fouling",
            fault_start="2025-07-08 00:00:00",
        )
    if case == "ncg_blanketing_only":
        return replace(
            config,
            seed=seed,
            fault_type="non_condensables",
            ncg_max_partial_pressure_kpa=0.0,
            ncg_blanketing_ua_loss=0.5,
        )
    return replace(config, seed=seed, **FAULT_CASES[case])


def _time_series_runs(
    config: SimulationConfig,
) -> tuple[
    pd.DataFrame,
    dict[int, tuple[float, float]],
    dict[tuple[str, int], tuple[float, float]],
]:
    statistic_rows = []
    healthy_baselines = {}
    baseline_fits = {}
    for seed in DETECTOR_SEEDS:
        healthy_frame = generate_telemetry(_case_config("healthy", seed, config))
        healthy_baselines[seed] = fit_baseline(healthy_frame, config)
        for case in TIMESERIES_CASES:
            frame = (
                healthy_frame
                if case == "healthy"
                else generate_telemetry(_case_config(case, seed, config))
            )
            baseline = fit_baseline(frame, config)
            baseline_fits[(case, seed)] = baseline
            statistic = detector_statistic(
                frame.loc[:, DETECTOR_INPUTS], baseline, config
            )
            statistic_rows.extend(
                {
                    "case": case,
                    "seed": seed,
                    "timestamp": timestamp,
                    "statistic": float(value),
                }
                for timestamp, value in statistic.items()
            )
    return pd.DataFrame(statistic_rows), healthy_baselines, baseline_fits


def _sweep_state(
    case: str,
    exponent: float,
    load: np.ndarray,
    cw_supply: np.ndarray,
    config: SimulationConfig,
    pressures: dict[str, float],
    references: dict[str, float],
):
    if case == "healthy":
        return solve_operating_point(
            load, cw_supply, config.ua_cond_kw_per_k, config
        )
    return sweeps._scenario_operating_point(
        case, exponent, load, cw_supply, config, pressures, references
    )


def _window_statistic(
    rng: np.random.Generator,
    load_kw: float,
    condenser_temp_c: float,
    chw_return_temp_c: float,
    discharge_pressure_kpa: float,
    baseline: tuple[float, float],
    config: SimulationConfig,
) -> float:
    size = WINDOW_SAMPLES
    measured_pressure = discharge_pressure_kpa + rng.normal(
        0.0, config.pressure_noise_sd_kpa, size
    )
    plr = load_kw / config.rated_capacity_kw
    subcooling = np.clip(
        config.cond_subcooling_c
        + config.subcooling_plr_gain_c * (plr - 0.65)
        + _ar1(
            rng.normal(size=size),
            config.subcooling_ar_phi,
            config.subcooling_ar_sd_c,
        ),
        0.2,
        None,
    )
    liquid_temperature = (
        condenser_temp_c
        - subcooling
        + rng.normal(0.0, config.temperature_noise_sd_c, size)
    )
    chw_flow = config.chw_flow_kg_s + rng.normal(
        0.0,
        config.chw_flow_kg_s * config.flow_noise_fraction,
        size,
    )
    chw_supply = config.chw_supply_setpoint_c + rng.normal(
        0.0, config.temperature_noise_sd_c, size
    )
    chw_return = chw_return_temp_c + rng.normal(
        0.0, config.temperature_noise_sd_c, size
    )
    measured_load = (
        chw_flow
        * config.cp_kj_per_kg_k
        * (chw_return - chw_supply)
    )
    apparent = (
        saturation_temperature_c(measured_pressure) - liquid_temperature
    )
    a, b = baseline
    residual = apparent - (
        a + b * measured_load / config.rated_capacity_kw
    )
    return float(np.mean(residual))


def _sweep_window_runs(
    config: SimulationConfig,
    healthy_baselines: dict[int, tuple[float, float]],
) -> pd.DataFrame:
    pressures, references = sweeps._calibrations(config)
    rows_by_chunk = []
    sweep_definitions = (
        (
            0,
            "load",
            sweeps.LOAD_POINTS_KW,
            sweeps.LOAD_POINTS_KW,
            np.full(len(sweeps.LOAD_POINTS_KW), sweeps.REFERENCE_CW_SUPPLY_C),
        ),
        (
            1,
            "cw",
            sweeps.CW_POINTS_C,
            np.full(len(sweeps.CW_POINTS_C), sweeps.REFERENCE_LOAD_KW),
            sweeps.CW_POINTS_C,
        ),
    )
    exponent_indices = {
        exponent: index for index, exponent in enumerate(sweeps.NCG_EXPONENTS)
    }

    for case in SWEEP_CASES:
        exponents = sweeps.NCG_EXPONENTS if case in sweeps.NCG_CASES else (0.0,)
        for exponent in exponents:
            exponent_index = exponent_indices.get(exponent, 0)
            chunk_rows = []
            for sweep_id, sweep_name, points, loads, cw_supply in sweep_definitions:
                healthy_state = solve_operating_point(
                    loads,
                    cw_supply,
                    config.ua_cond_kw_per_k,
                    config,
                )
                faulty_state = _sweep_state(
                    case,
                    exponent,
                    loads,
                    cw_supply,
                    config,
                    pressures,
                    references,
                )
                expected_excess = (
                    saturation_temperature_c(faulty_state.discharge_pressure_kpa)
                    - faulty_state.condenser_temp_c
                    - saturation_temperature_c(healthy_state.discharge_pressure_kpa)
                    + healthy_state.condenser_temp_c
                )
                for point_index, point_value in enumerate(points):
                    for seed in DETECTOR_SEEDS:
                        statistic = _window_statistic(
                            np.random.default_rng(
                                [
                                    seed,
                                    100 + sweep_id,
                                    SWEEP_CASE_INDEX[case],
                                    exponent_index,
                                    point_index,
                                ]
                            ),
                            float(loads[point_index]),
                            float(faulty_state.condenser_temp_c[point_index]),
                            float(faulty_state.chw_return_c[point_index]),
                            float(faulty_state.discharge_pressure_kpa[point_index]),
                            healthy_baselines[seed],
                            config,
                        )
                        chunk_rows.append(
                            {
                                "case": case,
                                "sweep": sweep_name,
                                "n": float(exponent),
                                "point_value": float(point_value),
                                "seed": seed,
                                "statistic": statistic,
                                "expected_excess_k": float(
                                    expected_excess[point_index]
                                ),
                                "ncg_partial_pressure_kpa": float(
                                    faulty_state.ncg_partial_pressure_kpa[
                                        point_index
                                    ]
                                ),
                            }
                        )
            rows_by_chunk.append(pd.DataFrame(chunk_rows))
    return pd.concat(rows_by_chunk, ignore_index=True)


def _calibration_populations(
    timeseries_statistics: pd.DataFrame,
    sweep_windows: pd.DataFrame,
) -> dict[str, np.ndarray]:
    calibration_cases = ("healthy", "condenser_fouling")
    time_series = timeseries_statistics.loc[
        timeseries_statistics["case"].isin(calibration_cases)
        & timeseries_statistics["seed"].isin(CALIBRATION_SEEDS),
        "statistic",
    ].to_numpy(dtype=float)
    sweep = sweep_windows.loc[
        sweep_windows["case"].isin(calibration_cases)
        & sweep_windows["seed"].isin(CALIBRATION_SEEDS),
        "statistic",
    ].to_numpy(dtype=float)
    return {
        "timeseries_hourly": time_series,
        "sweep_windows": sweep,
    }


def pooled_threshold(values: np.ndarray) -> float:
    values = np.asarray(values, dtype=float)
    mean = float(np.mean(values))
    sigma = float(np.std(values, ddof=1))
    return mean + THRESHOLD_Z * sigma


def _threshold_from_calibration(
    populations: dict[str, np.ndarray],
) -> dict[str, float]:
    population_stats = {}
    candidate_thresholds = []
    for name, values in populations.items():
        values = np.asarray(values, dtype=float)
        mean = float(np.mean(values))
        sigma = float(np.std(values, ddof=1))
        maximum = float(np.max(values))
        candidate = mean + THRESHOLD_Z * sigma
        population_stats[name] = {
            "mu_k": mean,
            "sigma_k": sigma,
            "max_k": maximum,
            "threshold_k": candidate,
            "count": float(len(values)),
        }
        candidate_thresholds.append(candidate)

    pooled = np.concatenate(
        [np.asarray(values, dtype=float) for values in populations.values()]
    )
    pooled_mean = float(np.mean(pooled))
    pooled_sigma = float(np.std(pooled, ddof=1))
    pooled_candidate = pooled_threshold(pooled)
    pooled_maximum = float(np.max(pooled))
    threshold = float(max(candidate_thresholds))
    if threshold <= pooled_maximum:
        raise ValueError(
            "detector threshold must exceed the pooled calibration maximum: "
            f"theta={threshold:.9g}, max={pooled_maximum:.9g}"
        )
    output = {
        "theta_k": threshold,
        "calibration_max_k": pooled_maximum,
        "pooled_mu_k": pooled_mean,
        "pooled_sigma_k": pooled_sigma,
        "pooled_theta_k": pooled_candidate,
    }
    for population, statistics in population_stats.items():
        for statistic, value in statistics.items():
            output[f"{population}_{statistic}"] = value
    return output


def _summarize_timeseries(
    statistics: pd.DataFrame,
    baseline_fits: dict[tuple[str, int], tuple[float, float]],
    threshold: float,
    config: SimulationConfig,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    alarms = statistics.loc[
        statistics["statistic"] > threshold
    ].copy()
    summaries = []
    delay_rows = []
    for (case, seed), group in statistics.groupby(["case", "seed"], sort=False):
        group_alarms = alarms.loc[
            (alarms["case"] == case) & (alarms["seed"] == seed)
        ]
        fault_start = pd.Timestamp(_case_config(case, seed, config).fault_start)
        pre_fault = group_alarms.loc[group_alarms["timestamp"] < fault_start]
        after_fault = group_alarms.loc[group_alarms["timestamp"] >= fault_start]
        first_alarm = (
            group_alarms["timestamp"].min()
            if not group_alarms.empty
            else pd.NaT
        )
        delay_hours = (
            float((first_alarm - fault_start).total_seconds() / 3600.0)
            if pd.notna(first_alarm)
            else np.nan
        )
        baseline_a, baseline_b = baseline_fits[(case, seed)]
        summaries.append(
            {
                "case": case,
                "seed": seed,
                "alarmed_evaluations": int(len(group_alarms)),
                "first_alarm_time": first_alarm,
                "delay_hours": delay_hours,
                "pre_fault_alarms": int(len(pre_fault)),
                "detected": bool(not after_fault.empty),
                "baseline_a": baseline_a,
                "baseline_b": baseline_b,
            }
        )

    result = pd.DataFrame(summaries)
    for case in NONZERO_NCG_CASES:
        case_rows = result.loc[result["case"] == case]
        detected = case_rows.loc[case_rows["detected"], "delay_hours"]
        delay_rows.append(
            {
                "case": case,
                "detected_count": int(case_rows["detected"].sum()),
                "run_count": int(len(case_rows)),
                "median_delay_hours": (
                    float(detected.median()) if not detected.empty else np.nan
                ),
                "max_delay_hours": (
                    float(detected.max()) if not detected.empty else np.nan
                ),
                "pre_fault_alarms": int(case_rows["pre_fault_alarms"].sum()),
            }
        )
    return result, pd.DataFrame(delay_rows)


def _summarize_sweeps(
    sweep_windows: pd.DataFrame, threshold: float
) -> pd.DataFrame:
    ncg_windows = sweep_windows.loc[
        sweep_windows["case"].isin(sweeps.NCG_CASES)
    ].copy()
    ncg_windows["alarmed"] = ncg_windows["statistic"] > threshold
    rows = []
    for keys, group in ncg_windows.groupby(
        ["case", "sweep", "n", "point_value"], sort=True
    ):
        case, sweep_name, exponent, point = keys
        count = int(group["alarmed"].sum())
        rows.append(
            {
                "case": case,
                "sweep": sweep_name,
                "n": float(exponent),
                "point_value": float(point),
                "detected_seeds": count,
                "seed_count": int(len(group)),
                "detection_rate": float(count / len(group)),
                "status": "detected" if count == len(group) else "missed",
                "expected_excess_k": float(group["expected_excess_k"].iloc[0]),
                "ncg_partial_pressure_kpa": float(
                    group["ncg_partial_pressure_kpa"].iloc[0]
                ),
                "threshold_k": float(threshold),
            }
        )
    return pd.DataFrame(rows)


def _false_alarm_table(
    timeseries_statistics: pd.DataFrame,
    sweep_windows: pd.DataFrame,
    threshold: float,
) -> pd.DataFrame:
    timeseries_alarms = timeseries_statistics.loc[
        timeseries_statistics["statistic"] > threshold
    ].copy()
    sweep_alarms = sweep_windows.loc[
        sweep_windows["statistic"] > threshold
    ]
    rows = []
    for case in FALSE_ALARM_CASES:
        for period, seeds in (
            ("calibration", CALIBRATION_SEEDS),
            ("held-out", HOLDOUT_SEEDS),
        ):
            ts = timeseries_alarms.loc[
                (timeseries_alarms["case"] == case)
                & timeseries_alarms["seed"].isin(seeds)
            ].copy()
            if ts.empty:
                alarmed_days = 0
            else:
                ts["date"] = ts["timestamp"].dt.date
                alarmed_days = int(
                    ts[["seed", "date"]].drop_duplicates().shape[0]
                )
            sweep_count = int(
                (
                    (sweep_alarms["case"] == case)
                    & sweep_alarms["seed"].isin(seeds)
                ).sum()
            )
            rows.append(
                {
                    "case": case,
                    "seed_group": period,
                    "seeds": len(seeds),
                    "time_series_alarmed_evaluations": int(len(ts)),
                    "time_series_alarmed_days": alarmed_days,
                    "sweep_alarmed_windows": sweep_count,
                }
            )
    return pd.DataFrame(rows)


def run_detector(
    config: SimulationConfig = SimulationConfig(),
) -> DetectorResults:
    timeseries_statistics, healthy_baselines, baseline_fits = _time_series_runs(
        config
    )
    sweep_windows = _sweep_window_runs(config, healthy_baselines)
    return _summarize_runs(
        timeseries_statistics,
        sweep_windows,
        healthy_baselines,
        baseline_fits,
        config,
    )


def _summarize_runs(
    timeseries_statistics: pd.DataFrame,
    sweep_windows: pd.DataFrame,
    healthy_baselines: dict[int, tuple[float, float]],
    baseline_fits: dict[tuple[str, int], tuple[float, float]],
    config: SimulationConfig,
) -> DetectorResults:
    calibration_populations = _calibration_populations(
        timeseries_statistics, sweep_windows
    )
    calibration_values = np.concatenate(
        list(calibration_populations.values())
    )
    threshold = _threshold_from_calibration(calibration_populations)
    timeseries_summary, delay_summary = _summarize_timeseries(
        timeseries_statistics,
        baseline_fits,
        threshold["theta_k"],
        config,
    )
    sweep_detection = _summarize_sweeps(
        sweep_windows, threshold["theta_k"]
    )
    false_alarms = _false_alarm_table(
        timeseries_statistics,
        sweep_windows,
        threshold["theta_k"],
    )
    return DetectorResults(
        threshold=threshold,
        calibration_populations=calibration_populations,
        calibration_values=calibration_values,
        timeseries_summary=timeseries_summary,
        timeseries_statistics=timeseries_statistics,
        sweep_detection=sweep_detection,
        sweep_windows=sweep_windows,
        false_alarms=false_alarms,
        delay_summary=delay_summary,
    )


def _corner_line(sweep_detection: pd.DataFrame, threshold: float) -> str:
    corner = sweep_detection.loc[
        (sweep_detection["case"] == "ncg_blanketed")
        & (sweep_detection["sweep"] == "cw")
        & (sweep_detection["point_value"] == sweeps.CW_POINTS_C[0])
        & (sweep_detection["n"] >= 9.0)
    ].sort_values("n")
    if corner.empty:
        return (
            "Known corner (ncg_blanketed, 22 °C CW inlet, n ≥ 9): "
            "unavailable — no matching rows."
        )
    status = (
        "missed"
        if corner["status"].eq("missed").all()
        else "detected"
    )
    rate_text = ", ".join(
        f"n={row.n:.1f}: {row.detection_rate:.0%}"
        for row in corner.itertuples(index=False)
    )
    expected_min = float(corner["expected_excess_k"].min())
    expected_max = float(corner["expected_excess_k"].max())
    expected_text = (
        f"{expected_min:.3f}–{expected_max:.3f}"
        if not np.isclose(expected_min, expected_max)
        else f"{expected_min:.3f}"
    )
    return (
        "Known corner (ncg_blanketed, 22 °C CW inlet, n ≥ 9): "
        f"{status} — detection rate {rate_text}, expected excess "
        f"{expected_text} K vs θ {threshold:.3f} K."
    )


def _missed_sweep_sections(
    sweep_detection: pd.DataFrame,
) -> tuple[list[str], str, list[str]]:
    missed = sweep_detection.loc[sweep_detection["status"] == "missed"]
    under_all = sweep_detection.loc[
        sweep_detection["expected_excess_k"]
        < sweep_detection["threshold_k"]
    ]
    if (
        missed["expected_excess_k"].eq(missed["threshold_k"]).any()
        or under_all["status"].eq("detected").any()
    ):
        raise ValueError("sweep misses cannot be classified at this threshold")

    under = missed.loc[
        missed["expected_excess_k"] < missed["threshold_k"]
    ].sort_values(["case", "sweep", "n", "point_value"])
    above = missed.loc[
        missed["expected_excess_k"] > missed["threshold_k"]
    ].sort_values(["case", "sweep", "n", "point_value"])
    if len(under) + len(above) != len(missed):
        raise ValueError("missed sweep row has no threshold classification")

    under_lines = [
        "| case | sweep | n | point value | expected excess (K) | "
        "detection rate | p_ncg (kPa) |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in under.itertuples(index=False):
        point_unit = "°C" if row.sweep == "cw" else "kW"
        under_lines.append(
            f"| {row.case} | {row.sweep} | {row.n:.1f} | "
            f"{row.point_value:g} {point_unit} | "
            f"{row.expected_excess_k:.3f} | {row.detection_rate:.1%} | "
            f"{row.ncg_partial_pressure_kpa:.3f} |"
        )

    above_lines = [
        "| case | sweep | n | point value | expected excess (K) | "
        "detection rate |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for row in above.itertuples(index=False):
        point_unit = "°C" if row.sweep == "cw" else "kW"
        above_lines.append(
            f"| {row.case} | {row.sweep} | {row.n:.1f} | "
            f"{row.point_value:g} {point_unit} | "
            f"{row.expected_excess_k:.3f} | {row.detection_rate:.1%} |"
        )

    zero_rate = int(missed["detection_rate"].eq(0.0).sum())
    count_line = (
        f"{len(missed)} missed rows: {len(under)} with expected excess "
        f"under θ (the known corner), {len(above)} partial detections with "
        f"expected excess above θ; missed rows with detection rate 0: "
        f"{zero_rate}."
    )
    return under_lines, count_line, above_lines


def _pass_fail_lines(results: DetectorResults) -> list[str]:
    threshold_valid = (
        results.threshold["theta_k"] > results.threshold["calibration_max_k"]
    )
    false_alarm_pass = (
        results.false_alarms[
            [
                "time_series_alarmed_evaluations",
                "time_series_alarmed_days",
                "sweep_alarmed_windows",
            ]
        ].to_numpy()
        == 0
    ).all()
    delay_status = []
    for row in results.delay_summary.itertuples(index=False):
        delay_status.append(
            row.detected_count == row.run_count and row.pre_fault_alarms == 0
        )
    no_prefault = results.delay_summary["pre_fault_alarms"].eq(0).all()
    corner = results.sweep_detection.loc[
        (results.sweep_detection["case"] == "ncg_blanketed")
        & (results.sweep_detection["sweep"] == "cw")
        & (results.sweep_detection["point_value"] == sweeps.CW_POINTS_C[0])
        & (results.sweep_detection["n"] >= 9.0)
    ]
    corner_pass = (
        not corner.empty and corner["status"].eq("missed").all()
    )
    lines = [
        f"- Threshold exceeds the pooled calibration maximum: "
        f"**{'PASS' if threshold_valid else 'FAIL'}**.",
        f"- Healthy, fouling, and blanketing-only false alarms are zero: "
        f"**{'PASS' if false_alarm_pass else 'FAIL'}**.",
        f"- Four nonzero-NCG time-series cases are detected 40/40: "
        f"**{'PASS' if all(delay_status) else 'FAIL'}**.",
        f"- NCG pre-fault alarms are zero: "
        f"**{'PASS' if no_prefault else 'FAIL'}**.",
        f"- Known corner is missed at every n ≥ 9 point: "
        f"**{'PASS' if corner_pass else 'FAIL'}**.",
    ]
    return lines


def detector_markdown(results: DetectorResults) -> str:
    threshold = results.threshold
    population_lines = [
        "| calibration population | n | μ (K) | σ (K) | max (K) | μ + 4σ (K) |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name in ("timeseries_hourly", "sweep_windows"):
        population_lines.append(
            f"| {name} | {int(threshold[f'{name}_count'])} | "
            f"{threshold[f'{name}_mu_k']:.6f} | "
            f"{threshold[f'{name}_sigma_k']:.6f} | "
            f"{threshold[f'{name}_max_k']:.6f} | "
            f"{threshold[f'{name}_threshold_k']:.6f} |"
        )
    false_lines = [
        "| case | seed group | seeds | time-series alarmed evaluations | "
        "alarmed days | sweep alarmed windows |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for row in results.false_alarms.itertuples(index=False):
        false_lines.append(
            f"| {row.case} | {row.seed_group} | {row.seeds} | "
            f"{row.time_series_alarmed_evaluations} | "
            f"{row.time_series_alarmed_days} | {row.sweep_alarmed_windows} |"
        )

    delay_lines = [
        "| NCG case | detected | median delay (h) | max delay (h) | "
        "pre-fault alarms | status |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in results.delay_summary.itertuples(index=False):
        passed = row.detected_count == row.run_count and row.pre_fault_alarms == 0
        median = (
            f"{row.median_delay_hours:.2f}"
            if np.isfinite(row.median_delay_hours)
            else "none"
        )
        maximum = (
            f"{row.max_delay_hours:.2f}"
            if np.isfinite(row.max_delay_hours)
            else "none"
        )
        delay_lines.append(
            f"| {row.case} | {row.detected_count}/{row.run_count} | "
            f"{median} | {maximum} | {row.pre_fault_alarms} | "
            f"{'PASS' if passed else 'FAIL'} |"
        )

    under_lines, missed_count_line, above_lines = _missed_sweep_sections(
        results.sweep_detection
    )
    corner = _corner_line(results.sweep_detection, threshold["theta_k"])
    independent = results.false_alarms.loc[
        results.false_alarms["seed_group"].eq("held-out")
        | (
            results.false_alarms["case"].eq("ncg_blanketing_only")
            & results.false_alarms["seed_group"].eq("calibration")
        )
    ]
    independent_evaluations = int(
        independent["time_series_alarmed_evaluations"].sum()
    )
    independent_days = int(independent["time_series_alarmed_days"].sum())
    independent_windows = int(independent["sweep_alarmed_windows"].sum())
    return "\n".join(
        [
            "#### Threshold recovery",
            "",
            "- Pre-stated pooled rule:",
            f"  - μ = {threshold['pooled_mu_k']:.6f} K",
            f"  - σ = {threshold['pooled_sigma_k']:.6f} K (ddof=1)",
            f"  - θ = μ + 4σ = {threshold['pooled_theta_k']:.6f} K",
            f"- Calibration maximum = {threshold['calibration_max_k']:.6f} K",
            "- Original pooled-rule gate: **FAILED** "
            f"(θ ≤ calibration maximum).",
            "",
            *population_lines,
            "",
            f"- Revised θ = max(population μ + 4σ) = "
            f"{threshold['theta_k']:.6f} K.",
            f"- Revised threshold gate (θ > pooled calibration maximum): "
            f"**{'PASS' if threshold['theta_k'] > threshold['calibration_max_k'] else 'FAIL'}**.",
            "",
            "#### False alarms",
            "",
            *false_lines,
            "",
            "Calibration-seed zeros for healthy and condenser_fouling are "
            "implied by the gate θ > calibration maximum "
            f"(θ = {threshold['theta_k']:.3f} K, "
            f"max = {threshold['calibration_max_k']:.3f} K); they are not "
            "an independent check.",
            "Independent false-alarm checks: held-out seeds 20–39 "
            "(healthy, condenser_fouling, ncg_blanketing_only) and "
            "ncg_blanketing_only on calibration seeds 0–19, which was not "
            "in the calibration population: "
            f"{independent_evaluations} alarmed evaluations, "
            f"{independent_days} alarmed days, "
            f"{independent_windows} alarmed windows.",
            "",
            "#### Time-series NCG detection delay",
            "",
            *delay_lines,
            "",
            "#### Missed: expected signal under θ",
            "",
            *under_lines,
            "",
            corner,
            "",
            "#### Partial detections: expected signal above θ "
            "(finite-sample failures of the 40/40 rule)",
            "",
            missed_count_line,
            "",
            *above_lines,
            "",
            "#### Pass/fail",
            "",
            *_pass_fail_lines(results),
        ]
    )


def _replace_generated_block(report_path: Path, block: str) -> None:
    report = report_path.read_text()
    if BEGIN_MARKER not in report or END_MARKER not in report:
        raise ValueError("ncg-detector generated markers are missing from report")
    before, rest = report.split(BEGIN_MARKER, maxsplit=1)
    _, after = rest.split(END_MARKER, maxsplit=1)
    report_path.write_text(
        f"{before}{BEGIN_MARKER}\n{block}\n{END_MARKER}{after}"
    )


def _plot_detector(results: DetectorResults, output_dir: Path) -> None:
    figure, axes = plt.subplots(1, 3, figsize=(19, 5.4))
    colors = {
        "healthy": "#252525",
        "condenser_fouling": "#CC79A7",
        "ncg_blanketing_only": "#009E73",
        "ncg_dalton": "#0072B2",
        "ncg_blanketed": "#D55E00",
        "ncg_dalton_ua_matched": "#56B4E9",
        "ncg_blanketed_ua_matched": "#E69F00",
    }
    labels = {
        "healthy": "Healthy",
        "condenser_fouling": "Fouling",
        "ncg_blanketing_only": "Blanketing-only",
        "ncg_dalton": "Dalton",
        "ncg_blanketed": "Blanketed",
        "ncg_dalton_ua_matched": "Dalton UA-matched",
        "ncg_blanketed_ua_matched": "Blanketed UA-matched",
    }

    statistics = results.timeseries_statistics.copy()
    origin = pd.Timestamp("2025-07-04 00:00:00")
    statistics["days_since_fault"] = (
        statistics["timestamp"] - origin
    ).dt.total_seconds() / 86400.0
    hourly = (
        statistics.groupby(["case", "days_since_fault"])["statistic"]
        .agg(median="median", minimum="min", maximum="max")
        .reset_index()
    )
    for case in PLOT_TIMESERIES_CASES:
        values = hourly.loc[hourly["case"] == case].sort_values(
            "days_since_fault"
        )
        axes[0].plot(
            values["days_since_fault"],
            values["median"],
            color=colors[case],
            label=labels[case],
        )
        axes[0].fill_between(
            values["days_since_fault"],
            values["minimum"],
            values["maximum"],
            color=colors[case],
            alpha=0.12,
        )
    axes[0].axhline(
        results.threshold["theta_k"],
        color="black",
        linestyle="--",
        label="θ",
    )
    axes[0].axvline(0.0, color="#555555", linestyle=":", label="Fault start")
    axes[0].set_title("(A) Time-series statistic S(t)")
    axes[0].set_xlabel("Days from fault start")
    axes[0].set_ylabel("Trailing residual mean S (K)")
    axes[0].legend(fontsize=7)
    axes[0].grid(alpha=0.2)

    cw22 = results.sweep_windows.loc[
        (results.sweep_windows["sweep"] == "cw")
        & np.isclose(results.sweep_windows["point_value"], 22.0)
    ]
    healthy_fouling = cw22.loc[
        cw22["case"].isin(("healthy", "condenser_fouling"))
    ]["statistic"]
    axes[1].axhspan(
        float(healthy_fouling.min()),
        float(healthy_fouling.max()),
        color="#999999",
        alpha=0.24,
        label="Healthy + fouling min–max",
    )
    for case in ("ncg_dalton", "ncg_blanketed"):
        values = (
            cw22.loc[cw22["case"] == case]
            .groupby("n")["statistic"]
            .agg(median="median", minimum="min", maximum="max")
            .reset_index()
            .sort_values("n")
        )
        axes[1].fill_between(
            values["n"],
            values["minimum"],
            values["maximum"],
            color=colors[case],
            alpha=0.16,
        )
        axes[1].plot(
            values["n"],
            values["median"],
            marker="o",
            color=colors[case],
            label=f"{labels[case]} S median/min–max",
        )
    expected = (
        results.sweep_detection.loc[
            (results.sweep_detection["case"] == "ncg_blanketed")
            & (results.sweep_detection["sweep"] == "cw")
            & (results.sweep_detection["point_value"] == 22.0)
        ]
        .sort_values("n")
    )
    axes[1].plot(
        expected["n"],
        expected["expected_excess_k"],
        color="#D55E00",
        linestyle="--",
        linewidth=1.7,
        label="Blanketed expected excess",
    )
    axes[1].axhline(
        results.threshold["theta_k"],
        color="black",
        linestyle=":",
        label="θ",
    )
    axes[1].axvspan(9.0, 10.25, color="#D55E00", alpha=0.08)
    axes[1].annotate(
        "missed",
        xy=(9.5, results.threshold["theta_k"]),
        xytext=(8.5, results.threshold["theta_k"] * 1.08),
        arrowprops={"arrowstyle": "->", "color": "#D55E00"},
        color="#D55E00",
    )
    axes[1].set_title("(B) CW sweep at 22 °C")
    axes[1].set_xlabel("NCG pressure exponent n")
    axes[1].set_ylabel("S / expected excess (K)")
    axes[1].legend(fontsize=7)
    axes[1].grid(alpha=0.2)

    heatmap = (
        results.sweep_detection.loc[
            (results.sweep_detection["case"] == "ncg_blanketed")
            & (results.sweep_detection["sweep"] == "cw")
        ]
        .pivot(index="point_value", columns="n", values="detection_rate")
        .sort_index()
        .sort_index(axis=1)
    )
    exponent_values = heatmap.columns.to_numpy(dtype=float)
    cw_values = heatmap.index.to_numpy(dtype=float)
    image = axes[2].imshow(
        heatmap.to_numpy(dtype=float),
        origin="lower",
        aspect="auto",
        interpolation="nearest",
        extent=(
            exponent_values[0] - 0.25,
            exponent_values[-1] + 0.25,
            cw_values[0] - 0.5,
            cw_values[-1] + 0.5,
        ),
        vmin=0.0,
        vmax=1.0,
        cmap="viridis",
    )
    axes[2].add_patch(
        plt.Rectangle(
            (8.75, 21.5),
            1.5,
            1.0,
            fill=False,
            edgecolor="red",
            linewidth=2.0,
        )
    )
    axes[2].set_title("(C) Blanketed CW detection rate")
    axes[2].set_xlabel("NCG pressure exponent n")
    axes[2].set_ylabel("CW inlet temperature (°C)")
    figure.colorbar(image, ax=axes[2], label="Detection rate")
    figure.suptitle("Apparent-subcooling trend-and-threshold detector")
    figure.tight_layout()
    figure.savefig(output_dir / "ncg_detector_threshold.png", dpi=160)
    plt.close(figure)


def _write_timeseries_csv(
    frame: pd.DataFrame, output_path: Path
) -> None:
    columns = [
        "case",
        "seed",
        "baseline_a",
        "baseline_b",
        "alarmed_evaluations",
        "first_alarm_time",
        "delay_hours",
    ]
    frame.loc[:, columns].to_csv(output_path, index=False)


def generate_detector_report(
    output_dir: str | Path = "reports",
    report_path: str | Path = "diagnostic_report.md",
    results: DetectorResults | None = None,
) -> DetectorResults:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if results is None:
        results = run_detector()
    _write_timeseries_csv(
        results.timeseries_summary,
        output_dir / "ncg_detector_timeseries.csv",
    )
    results.sweep_detection.to_csv(
        output_dir / "ncg_detector_sweep.csv", index=False
    )
    _plot_detector(results, output_dir)
    _replace_generated_block(Path(report_path), detector_markdown(results))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate a trend-and-threshold NCG detector using apparent "
            "subcooling only."
        )
    )
    parser.add_argument("--output-dir", default="reports")
    parser.add_argument("--report", default="diagnostic_report.md")
    args = parser.parse_args()
    generate_detector_report(args.output_dir, args.report)


if __name__ == "__main__":
    main()
