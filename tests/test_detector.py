from io import StringIO
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from chiller_sim import REQUIRED_COLUMNS, SimulationConfig, generate_telemetry
from chiller_sim.diagnostics import LABEL_COLUMNS, derive_signals
from chiller_sim.detector import (
    BEGIN_MARKER,
    DETECTOR_INPUTS,
    END_MARKER,
    FALSE_ALARM_CASES,
    NONZERO_NCG_CASES,
    SECTION_HEADING,
    _corner_line,
    apparent_subcooling,
    detector_markdown,
    detector_statistic,
    fit_baseline,
    pooled_threshold,
    run_detector,
)


@pytest.fixture(scope="module")
def detector_results():
    return run_detector()


def test_detector_uses_only_declared_inputs_and_matches_derived_subcooling() -> None:
    telemetry = generate_telemetry(SimulationConfig(seed=42))
    inputs = telemetry.loc[:, DETECTOR_INPUTS]
    baseline = fit_baseline(inputs)
    statistic = detector_statistic(inputs, baseline)
    derived = derive_signals(telemetry)

    np.testing.assert_allclose(
        apparent_subcooling(inputs),
        derived["apparent_subcooling_c"].to_numpy(),
        atol=1e-12,
        rtol=0.0,
    )
    assert statistic.index[0] == pd.Timestamp("2025-07-04 00:00:00")
    assert statistic.index[-1] == pd.Timestamp("2025-07-07 23:00:00")
    assert len(statistic) == 96


def test_blanketing_only_time_series_sensors_match_fouling() -> None:
    fouling = generate_telemetry(SimulationConfig(seed=7))
    blanketing_only = generate_telemetry(
        SimulationConfig(
            seed=7,
            fault_type="non_condensables",
            ncg_max_partial_pressure_kpa=0.0,
            ncg_blanketing_ua_loss=0.5,
        )
    )
    sensor_columns = [
        column
        for column in REQUIRED_COLUMNS
        if column not in LABEL_COLUMNS and column != "timestamp"
    ]
    np.testing.assert_allclose(
        fouling[sensor_columns].to_numpy(),
        blanketing_only[sensor_columns].to_numpy(),
        atol=1e-9,
        rtol=0.0,
    )


def test_prestated_pooled_threshold_fails_the_calibration_gate(
    detector_results,
) -> None:
    threshold = detector_results.threshold
    pooled = pooled_threshold(detector_results.calibration_values)
    assert len(detector_results.calibration_populations["timeseries_hourly"]) == 3840
    assert len(detector_results.calibration_populations["sweep_windows"]) == 1000
    assert pooled == pytest.approx(threshold["pooled_theta_k"])
    assert pooled == pytest.approx(0.514697355, abs=5e-8)
    assert threshold["calibration_max_k"] == pytest.approx(
        0.520461739, abs=5e-8
    )
    assert pooled <= threshold["calibration_max_k"]


def test_revised_threshold_recomputes_per_population_and_exceeds_max(
    detector_results,
) -> None:
    threshold = detector_results.threshold
    expected = []
    for name, values in detector_results.calibration_populations.items():
        assert threshold[f"{name}_mu_k"] == pytest.approx(float(np.mean(values)))
        assert threshold[f"{name}_sigma_k"] == pytest.approx(
            float(np.std(values, ddof=1))
        )
        assert threshold[f"{name}_max_k"] == pytest.approx(float(np.max(values)))
        population_threshold = (
            float(np.mean(values)) + 4.0 * float(np.std(values, ddof=1))
        )
        assert threshold[f"{name}_threshold_k"] == pytest.approx(
            population_threshold
        )
        expected.append(population_threshold)
    assert threshold["calibration_max_k"] == pytest.approx(
        max(
            float(np.max(values))
            for values in detector_results.calibration_populations.values()
        )
    )
    assert threshold["theta_k"] == pytest.approx(max(expected))
    assert threshold["theta_k"] > threshold["calibration_max_k"]


def test_false_alarm_guard_for_control_cases(detector_results) -> None:
    false_alarms = detector_results.false_alarms
    assert set(false_alarms["case"]) == set(FALSE_ALARM_CASES)
    assert set(false_alarms["seed_group"]) == {"calibration", "held-out"}
    assert false_alarms[
        [
            "time_series_alarmed_evaluations",
            "time_series_alarmed_days",
            "sweep_alarmed_windows",
        ]
    ].to_numpy().sum() == 0


def test_all_ncg_time_series_cases_detect_without_prefault_alarms(
    detector_results,
) -> None:
    delays = detector_results.delay_summary.set_index("case")
    for case in NONZERO_NCG_CASES:
        assert delays.loc[case, "detected_count"] == 40
        assert delays.loc[case, "run_count"] == 40
        assert delays.loc[case, "pre_fault_alarms"] == 0


def test_known_corner_is_missed_and_reported_as_missed(detector_results) -> None:
    corner = detector_results.sweep_detection.loc[
        (detector_results.sweep_detection["case"] == "ncg_blanketed")
        & (detector_results.sweep_detection["sweep"] == "cw")
        & (detector_results.sweep_detection["point_value"] == 22.0)
        & (detector_results.sweep_detection["n"] >= 9.0)
    ]
    assert len(corner) == 3
    assert corner["status"].eq("missed").all()
    corner_line = _corner_line(
        detector_results.sweep_detection,
        detector_results.threshold["theta_k"],
    )
    assert corner_line.startswith(
        "Known corner (ncg_blanketed, 22 °C CW inlet, n ≥ 9): missed —"
    )
    assert corner_line in detector_markdown(detector_results)


def test_detector_csv_report_block_and_plot_match_recompute(
    detector_results,
) -> None:
    root = Path(__file__).resolve().parents[1]
    report = (root / "diagnostic_report.md").read_text()
    for heading in (
        SECTION_HEADING,
        "### Threshold rule failure and revision",
        "### Prediction vs outcome",
        "### Limits",
    ):
        assert heading in report

    generated = report.split(BEGIN_MARKER, maxsplit=1)[1].split(
        END_MARKER, maxsplit=1
    )[0].strip()
    assert generated == detector_markdown(detector_results)

    timeseries_buffer = StringIO()
    detector_results.timeseries_summary.loc[
        :,
        [
            "case",
            "seed",
            "baseline_a",
            "baseline_b",
            "alarmed_evaluations",
            "first_alarm_time",
            "delay_hours",
        ],
    ].to_csv(timeseries_buffer, index=False)
    assert (
        (root / "reports/ncg_detector_timeseries.csv").read_bytes()
        == timeseries_buffer.getvalue().encode()
    )

    sweep_buffer = StringIO()
    detector_results.sweep_detection.to_csv(sweep_buffer, index=False)
    assert (
        (root / "reports/ncg_detector_sweep.csv").read_bytes()
        == sweep_buffer.getvalue().encode()
    )
    plot = root / "reports/ncg_detector_threshold.png"
    assert plot.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
