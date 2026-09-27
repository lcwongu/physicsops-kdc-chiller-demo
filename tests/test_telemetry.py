from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from chiller_sim import (
    REQUIRED_COLUMNS,
    SimulationConfig,
    generate_telemetry,
    save_telemetry,
)
from chiller_sim.diagnostics import derive_signals


@pytest.fixture(scope="module")
def default_df() -> pd.DataFrame:
    return generate_telemetry()


def test_reproducibility_and_committed_csv(
    default_df: pd.DataFrame, tmp_path: Path
) -> None:
    pd.testing.assert_frame_equal(default_df, generate_telemetry())
    assert not default_df.equals(generate_telemetry(SimulationConfig(seed=43)))

    first_path = tmp_path / "first.csv"
    second_path = tmp_path / "nested" / "second.csv"
    save_telemetry(default_df, first_path)
    save_telemetry(default_df, second_path)
    assert first_path.read_bytes() == second_path.read_bytes()

    csv_path = Path(__file__).resolve().parents[1] / "data/chiller_telemetry.csv"
    committed = pd.read_csv(csv_path, parse_dates=["timestamp"])
    pd.testing.assert_frame_equal(
        committed,
        default_df.round(4),
        check_exact=False,
        atol=1e-6,
        check_dtype=False,
    )


def test_required_columns_and_no_missing_values(default_df: pd.DataFrame) -> None:
    assert list(default_df.columns) == list(REQUIRED_COLUMNS)
    assert not default_df.isna().any().any()
    assert all(
        pd.api.types.is_numeric_dtype(default_df[column])
        for column in default_df.columns
        if column != "timestamp"
    )


def test_time_span(default_df: pd.DataFrame) -> None:
    timestamps = default_df["timestamp"]
    assert len(default_df) == 2016
    assert timestamps.iloc[0] == pd.Timestamp("2025-07-01 00:00")
    assert timestamps.iloc[-1] == pd.Timestamp("2025-07-07 23:55")
    assert (timestamps.diff().dropna() == pd.Timedelta(minutes=5)).all()
    assert timestamps.iloc[-1] - timestamps.iloc[0] == (
        pd.Timedelta(days=7) - pd.Timedelta(minutes=5)
    )


def test_fault_schedule(default_df: pd.DataFrame) -> None:
    fault_start = pd.Timestamp("2025-07-04 00:00")
    before_fault = default_df["timestamp"] < fault_start
    assert (default_df.loc[before_fault, "fault_severity"] == 0.0).all()
    assert (default_df.loc[before_fault, "fault_active"] == 0).all()
    assert default_df["fault_severity"].is_monotonic_increasing
    assert default_df["fault_severity"].iloc[-1] == pytest.approx(0.5)
    np.testing.assert_allclose(
        default_df["condenser_ua_kw_per_k"],
        200.0 * (1.0 - default_df["fault_severity"]),
    )


def _hour_matched_delta(
    df: pd.DataFrame, column: str, healthy: pd.Series, degraded: pd.Series
) -> float:
    hourly_healthy = df.loc[healthy].groupby(df.loc[healthy, "timestamp"].dt.hour)[
        column
    ].mean()
    hourly_degraded = df.loc[degraded].groupby(
        df.loc[degraded, "timestamp"].dt.hour
    )[column].mean()
    return float((hourly_degraded - hourly_healthy).mean())


def _assert_degradation_direction(df: pd.DataFrame) -> None:
    fault_start = pd.Timestamp("2025-07-04 00:00")
    healthy = df["timestamp"] < fault_start
    degraded = df["timestamp"] >= (
        df["timestamp"].iloc[-1] - pd.Timedelta(days=1)
    )
    signals = [
        "discharge_pressure_kpa",
        "compressor_power_kw",
        "cop",
        "cw_return_temp_c",
        "suction_pressure_kpa",
        "chw_supply_temp_c",
        "cooling_load_kw",
    ]
    deltas = {
        column: _hour_matched_delta(df, column, healthy, degraded)
        for column in signals
    }
    healthy_means = {
        column: float(
            df.loc[healthy]
            .groupby(df.loc[healthy, "timestamp"].dt.hour)[column]
            .mean()
            .mean()
        )
        for column in signals
    }

    assert deltas["discharge_pressure_kpa"] >= 40.0
    assert deltas["compressor_power_kw"] / healthy_means["compressor_power_kw"] >= 0.05
    assert deltas["cop"] / healthy_means["cop"] <= -0.05
    assert deltas["cw_return_temp_c"] > 0.0
    assert abs(deltas["suction_pressure_kpa"]) < 10.0
    assert abs(deltas["chw_supply_temp_c"]) < 0.1
    assert abs(deltas["cooling_load_kw"] / healthy_means["cooling_load_kw"]) < 0.03


def test_degradation_direction(default_df: pd.DataFrame) -> None:
    _assert_degradation_direction(default_df)


@pytest.mark.parametrize("seed", range(50))
def test_degradation_direction_across_seeds(seed: int) -> None:
    _assert_degradation_direction(generate_telemetry(SimulationConfig(seed=seed)))


def test_physical_coherence(default_df: pd.DataFrame) -> None:
    assert (
        default_df["chw_return_temp_c"] > default_df["chw_supply_temp_c"]
    ).all()
    assert (default_df["cw_return_temp_c"] > default_df["cw_supply_temp_c"]).all()
    assert (
        default_df["discharge_pressure_kpa"] > default_df["suction_pressure_kpa"]
    ).all()

    condenser_heat = (
        default_df["cw_flow_kg_s"]
        * 4.186
        * (default_df["cw_return_temp_c"] - default_df["cw_supply_temp_c"])
    )
    relative_error = (
        (condenser_heat - default_df["cooling_load_kw"] - default_df["compressor_power_kw"])
        / (default_df["cooling_load_kw"] + default_df["compressor_power_kw"])
    ).abs()
    assert relative_error.mean() < 0.03
    np.testing.assert_allclose(
        default_df["cop"],
        default_df["cooling_load_kw"] / default_df["compressor_power_kw"],
        rtol=1e-9,
    )
