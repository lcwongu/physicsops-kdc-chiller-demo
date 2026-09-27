from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from chiller_sim import (
    NCG_PARTIAL_PRESSURE_P_A_KPA,
    NCG_PARTIAL_PRESSURE_P_B_KPA,
    NCG_PARTIAL_PRESSURE_P_C_KPA,
    NCG_PARTIAL_PRESSURE_P_D_KPA,
    REQUIRED_COLUMNS,
    SimulationConfig,
    generate_telemetry,
)
from chiller_sim.diagnostics import (
    LABEL_COLUMNS,
    compare_periods,
    diagnose,
)
from chiller_sim.separation import (
    BEGIN_MARKER,
    END_MARKER,
    FAULT_CASES,
    REFERENCE_CASE,
    SEPARATION_METRICS,
    SEPARATION_SEEDS,
    case_metrics,
    interval_gap,
    metric_distributions,
    separability,
    separation_claims,
    separation_markdown,
)


@pytest.fixture(scope="module")
def distribution() -> pd.DataFrame:
    return metric_distributions()


def _seed42_diagnoses() -> dict[str, str]:
    results = {}
    for case, overrides in FAULT_CASES.items():
        config = SimulationConfig(seed=42, **overrides)
        telemetry = generate_telemetry(config)
        results[case] = diagnose(compare_periods(telemetry, config)).mechanism
    return results


def test_pre_fault_sensor_baselines_match() -> None:
    fouling = generate_telemetry(SimulationConfig(seed=42))
    ncg = generate_telemetry(
        SimulationConfig(seed=42, fault_type="non_condensables")
    )
    before_fault = fouling["timestamp"] < pd.Timestamp(
        SimulationConfig().fault_start
    )
    sensor_columns = [
        column
        for column in REQUIRED_COLUMNS
        if column not in LABEL_COLUMNS and column != "timestamp"
    ]
    np.testing.assert_allclose(
        fouling.loc[before_fault, sensor_columns].to_numpy(),
        ncg.loc[before_fault, sensor_columns].to_numpy(),
        atol=1e-9,
    )


def test_ncg_labels_and_pressure_ramp() -> None:
    ncg = generate_telemetry(
        SimulationConfig(seed=42, fault_type="non_condensables")
    )
    fouling = generate_telemetry(SimulationConfig(seed=42))
    np.testing.assert_allclose(ncg["condenser_ua_kw_per_k"], 200.0)
    np.testing.assert_allclose(
        ncg["ncg_partial_pressure_kpa"],
        NCG_PARTIAL_PRESSURE_P_A_KPA * ncg["fault_severity"],
    )
    assert ncg["ncg_partial_pressure_kpa"].iloc[0] == 0.0
    assert ncg["ncg_partial_pressure_kpa"].iloc[-1] == pytest.approx(
        NCG_PARTIAL_PRESSURE_P_A_KPA
    )
    assert (fouling["ncg_partial_pressure_kpa"] == 0.0).all()
    assert SimulationConfig().ncg_max_partial_pressure_kpa == pytest.approx(
        NCG_PARTIAL_PRESSURE_P_A_KPA
    )
    assert (
        FAULT_CASES["ncg_blanketed"]["ncg_max_partial_pressure_kpa"]
        == NCG_PARTIAL_PRESSURE_P_B_KPA
    )


def test_unknown_fault_type_raises() -> None:
    with pytest.raises(ValueError, match="unsupported fault_type"):
        generate_telemetry(SimulationConfig(fault_type="unknown"))


def test_seed42_discharge_pressure_calibration() -> None:
    fouling_config = SimulationConfig(seed=42)
    fouling = generate_telemetry(fouling_config)
    target = case_metrics(fouling, fouling_config)[
        "discharge_pressure_delta_kpa"
    ]
    for overrides in (
        FAULT_CASES["ncg_dalton"],
        FAULT_CASES["ncg_blanketed"],
    ):
        config = SimulationConfig(seed=42, **overrides)
        metrics = case_metrics(generate_telemetry(config), config)
        assert abs(metrics["discharge_pressure_delta_kpa"] - target) <= 3.0


def test_seed42_ua_matched_calibration_within_half_percentage_point() -> None:
    fouling_config = SimulationConfig(seed=42)
    fouling_metrics = case_metrics(
        generate_telemetry(fouling_config), fouling_config
    )
    target = fouling_metrics["ua_cond_est_pct"]
    for case, pressure in (
        ("ncg_dalton_ua_matched", NCG_PARTIAL_PRESSURE_P_C_KPA),
        ("ncg_blanketed_ua_matched", NCG_PARTIAL_PRESSURE_P_D_KPA),
    ):
        config = SimulationConfig(seed=42, **FAULT_CASES[case])
        metrics = case_metrics(generate_telemetry(config), config)
        assert config.ncg_max_partial_pressure_kpa == pressure
        assert abs(metrics["ua_cond_est_pct"] - target) <= 0.5


def test_interval_gap_overlap_touching_and_disjoint() -> None:
    assert interval_gap(np.array([0.0, 1.0]), np.array([0.5, 1.5])) < 0.0
    assert interval_gap(np.array([0.0, 1.0]), np.array([1.0, 2.0])) == 0.0
    assert interval_gap(np.array([0.0, 1.0]), np.array([1.1, 2.0])) > 0.0


def test_overlapping_metric_ranges_are_not_claimed() -> None:
    rows = []
    for metric, _ in SEPARATION_METRICS:
        for case in FAULT_CASES:
            rows.append({"case": case, "seed": 0, metric: 1.0})
    dist = pd.DataFrame(rows)
    sep = separability(dist)
    claims = separation_claims(sep)
    assert not sep["separable"].any()
    assert not claims["separable_vs_all_ncg_variants"].any()


def test_disjoint_discharge_calibrations_do_not_override_ua_match_overlap() -> None:
    ua_ranges = {
        REFERENCE_CASE: (0.0, 1.0),
        "ncg_dalton": (-2.0, -1.0),
        "ncg_blanketed": (2.0, 3.0),
        "ncg_dalton_ua_matched": (0.5, 1.5),
        "ncg_blanketed_ua_matched": (3.0, 4.0),
    }
    rows = []
    for case, (low, high) in ua_ranges.items():
        for seed, value in enumerate((low, high)):
            row = {"case": case, "seed": seed}
            for metric, _ in SEPARATION_METRICS:
                row[metric] = (
                    value if metric == "ua_cond_est_pct" else float(seed)
                )
            rows.append(row)
    sep = separability(pd.DataFrame(rows))
    claims = separation_claims(sep).set_index("metric")
    ua_rows = sep.loc[sep["metric"] == "ua_cond_est_pct"].set_index("case")

    assert bool(ua_rows.loc["ncg_dalton", "separable"])
    assert bool(ua_rows.loc["ncg_blanketed", "separable"])
    assert not bool(ua_rows.loc["ncg_dalton_ua_matched", "separable"])
    assert not bool(
        claims.loc["ua_cond_est_pct", "separable_vs_all_ncg_variants"]
    )


def test_seed_ranges_and_claims_match_distribution(
    distribution: pd.DataFrame,
) -> None:
    assert tuple(sorted(distribution["seed"].unique())) == SEPARATION_SEEDS
    sep = separability(distribution)
    for _, row in sep.iterrows():
        reference = distribution.loc[
            distribution["case"] == REFERENCE_CASE, row["metric"]
        ]
        case_values = distribution.loc[
            distribution["case"] == row["case"], row["metric"]
        ]
        assert row["ref_min"] == pytest.approx(reference.min(), abs=1e-12)
        assert row["ref_max"] == pytest.approx(reference.max(), abs=1e-12)
        assert row["case_min"] == pytest.approx(case_values.min(), abs=1e-12)
        assert row["case_max"] == pytest.approx(case_values.max(), abs=1e-12)
        expected_gap = interval_gap(reference.to_numpy(), case_values.to_numpy())
        assert row["gap"] == pytest.approx(expected_gap, abs=1e-12)
        assert bool(row["separable"]) == (expected_gap > 0.0)

    root = Path(__file__).resolve().parents[1]
    committed = pd.read_csv(root / "reports/fault_separation_summary.csv")
    pd.testing.assert_frame_equal(
        committed,
        sep,
        check_exact=False,
        atol=1e-9,
        rtol=0.0,
        check_dtype=False,
    )
    committed_metrics = pd.read_csv(root / "reports/fault_separation_metrics.csv")
    pd.testing.assert_frame_equal(
        committed_metrics,
        distribution,
        check_exact=False,
        atol=1e-9,
        rtol=0.0,
        check_dtype=False,
    )


def test_generated_separation_report_matches_claims(
    distribution: pd.DataFrame,
) -> None:
    root = Path(__file__).resolve().parents[1]
    sep = separability(distribution)
    claims = separation_claims(sep)
    diagnoses = _seed42_diagnoses()
    expected = separation_markdown(distribution, sep, claims, diagnoses)
    report = (root / "diagnostic_report.md").read_text()
    start = report.index(BEGIN_MARKER) + len(BEGIN_MARKER)
    end = report.index(END_MARKER)
    assert report[start:end].strip() == expected

    claimed_line = next(
        line
        for line in expected.splitlines()
        if line.startswith("Claimed separators")
    )
    claimed_text = claimed_line.split(": ", maxsplit=1)[1].rstrip(".")
    claimed = [] if claimed_text == "none" else claimed_text.split(", ")
    claim_map = claims.set_index("metric")["separable_vs_all_ncg_variants"]
    assert set(claimed) == set(claim_map[claim_map].index)
    for metric in claimed:
        assert sep.loc[sep["metric"] == metric, "separable"].all()
    assert "discharge_pressure_delta_kpa" not in claimed
    assert "ua_cond_est_pct" not in claimed

    for heading in (
        "## Second fault: non-condensable gas vs condenser fouling",
        "### Remaining ambiguity",
    ):
        assert heading in report
    for filename in (
        "fault_comparison_timeseries.png",
        "fault_separation_ranges.png",
        "fault_load_signature.png",
    ):
        image = root / "reports" / filename
        assert image.is_file()
        assert image.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_seed42_diagnosis_is_sensor_classifier_output() -> None:
    expected = _seed42_diagnoses()
    for case, overrides in FAULT_CASES.items():
        config = SimulationConfig(seed=42, **overrides)
        comparison = compare_periods(generate_telemetry(config), config)
        assert diagnose(comparison).mechanism == expected[case]
