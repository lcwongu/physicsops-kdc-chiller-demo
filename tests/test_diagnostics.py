import re
import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from chiller_sim import (
    SimulationConfig,
    generate_telemetry,
    saturation_pressure_kpa,
    saturation_temperature_c,
)
from chiller_sim.diagnostics import (
    LABEL_COLUMNS,
    compare_periods,
    derive_signals,
    diagnose,
    measured_changes_markdown,
    trend_slopes,
)
from chiller_sim.report import (
    BEGIN_MARKER,
    END_MARKER,
    generate_report,
)


@pytest.fixture(scope="module")
def committed_df() -> pd.DataFrame:
    csv_path = Path(__file__).resolve().parents[1] / "data/chiller_telemetry.csv"
    return pd.read_csv(csv_path, parse_dates=["timestamp"])


def test_saturation_pressure_round_trip() -> None:
    temperatures = np.array([0.0, 20.0, 40.0])
    np.testing.assert_allclose(
        saturation_temperature_c(saturation_pressure_kpa(temperatures)),
        temperatures,
        atol=1e-12,
    )


def test_derive_signals_uses_sensor_columns_only(
    committed_df: pd.DataFrame,
) -> None:
    derived = derive_signals(committed_df)
    assert not set(LABEL_COLUMNS).intersection(derived.columns)
    assert "ua_cond_est_kw_per_k" in derived
    assert "lift_c" in derived


def test_daily_sensor_ua_matches_ground_truth(
    committed_df: pd.DataFrame,
) -> None:
    derived = derive_signals(committed_df)
    derived["day"] = derived["timestamp"].dt.date
    truth = committed_df.copy()
    truth["day"] = truth["timestamp"].dt.date
    estimate_by_day = derived.groupby("day")["ua_cond_est_kw_per_k"].mean()
    truth_by_day = truth.groupby("day")["condenser_ua_kw_per_k"].mean()
    relative_error = (estimate_by_day - truth_by_day).abs() / truth_by_day
    assert (relative_error < 0.03).all()


def test_compare_periods_signatures(committed_df: pd.DataFrame) -> None:
    comparison = compare_periods(committed_df)
    assert comparison.loc["ua_cond_est_kw_per_k", "pct_change"] <= -25.0
    assert comparison.loc["condenser_approach_c", "delta"] >= 1.0
    assert comparison.loc["discharge_pressure_kpa", "delta"] >= 40.0
    assert comparison.loc["compressor_power_kw", "pct_change"] >= 5.0
    assert comparison.loc["cop", "pct_change"] <= -5.0


def test_load_and_weather_adjusted_trends(committed_df: pd.DataFrame) -> None:
    trends = trend_slopes(committed_df)
    ua_slopes = trends.loc["ua_cond_est_kw_per_k"]
    assert -30.0 <= ua_slopes["fault_slope_per_day"] <= -20.0
    assert abs(ua_slopes["healthy_slope_per_day"]) < 2.0
    assert trends.loc["discharge_pressure_kpa", "fault_slope_per_day"] > 0.0


def test_diagnosis_is_sensor_only_and_checks_alternatives(
    committed_df: pd.DataFrame,
) -> None:
    full_diagnosis = diagnose(compare_periods(committed_df))
    sensor_only = committed_df.drop(columns=list(LABEL_COLUMNS))
    stripped_diagnosis = diagnose(compare_periods(sensor_only))
    assert full_diagnosis == stripped_diagnosis
    assert full_diagnosis.degraded
    assert (
        full_diagnosis.mechanism
        == "condenser heat-transfer degradation (waterside fouling/scaling)"
    )
    hypotheses = {check.name: check for check in full_diagnosis.hypotheses}
    assert hypotheses["Condenser heat-transfer degradation"].consistent
    assert hypotheses["Non-condensables in the condenser"].consistent
    assert "can't tell it apart from fouling" in hypotheses[
        "Non-condensables in the condenser"
    ].note
    assert not hypotheses["Elevated CW supply / tower-weather"].consistent
    assert not hypotheses["Reduced CW flow"].consistent
    assert not hypotheses["Evaporator-side fault or refrigerant undercharge"].consistent
    assert not hypotheses["Increased cooling load"].consistent


def test_zero_fault_diagnosis() -> None:
    healthy = generate_telemetry(SimulationConfig(max_fault_severity=0.0))
    result = diagnose(compare_periods(healthy))
    assert not result.degraded
    assert result.mechanism == "no significant degradation"


def test_generate_report_and_summary(
    committed_df: pd.DataFrame, tmp_path: Path
) -> None:
    root = Path(__file__).resolve().parents[1]
    source_report = root / "diagnostic_report.md"
    copied_report = tmp_path / "diagnostic_report.md"
    shutil.copyfile(source_report, copied_report)

    output_dir = tmp_path / "reports"
    generate_report(committed_df, output_dir, copied_report)
    for filename in (
        "diagnostic_timeseries.png",
        "diagnostic_hourly_profiles.png",
        "diagnostic_changes.png",
    ):
        image_path = output_dir / filename
        assert image_path.is_file()
        assert image_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")

    summary = pd.read_csv(output_dir / "diagnostic_summary.csv")
    assert list(summary.columns) == [
        "signal",
        "healthy_mean",
        "degraded_mean",
        "delta",
        "pct_change",
        "healthy_slope_per_day",
        "fault_slope_per_day",
    ]
    comparison = compare_periods(committed_df)
    trends = trend_slopes(committed_df)
    text = copied_report.read_text()
    expected_table = measured_changes_markdown(comparison, trends)
    generated_start = text.index(BEGIN_MARKER) + len(BEGIN_MARKER)
    generated_end = text.index(END_MARKER)
    assert text[generated_start:generated_end].strip() == expected_table


def test_diagnostic_report_draft_and_referenced_plots() -> None:
    root = Path(__file__).resolve().parents[1]
    report_path = root / "diagnostic_report.md"
    assert report_path.is_file()
    text = report_path.read_text()
    for heading in (
        "Assumptions",
        "Fault schedule",
        "Measured changes",
        "Diagnostic reasoning",
        "Limitations",
        "Test weakness and recovery",
    ):
        assert f"## {heading}" in text
    assert "accelerated" in text.lower()
    for relative_path in re.findall(r"!\[[^\]]*\]\(([^)]+\.png)\)", text):
        assert (root / relative_path).is_file()


def test_measured_changes_markdown_is_deterministic(
    committed_df: pd.DataFrame,
) -> None:
    comparison = compare_periods(committed_df)
    trends = trend_slopes(committed_df)
    first = measured_changes_markdown(comparison, trends)
    second = measured_changes_markdown(comparison.copy(), trends.copy())
    assert first == second
