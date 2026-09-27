from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from chiller_sim import SimulationConfig, saturation_pressure_kpa, solve_operating_point
from chiller_sim.sweeps import (
    BEGIN_MARKER,
    CASE_INDEX,
    CW_POINTS_C,
    LOAD_ELASTICITY,
    LOAD_POINTS_KW,
    NCG_CASES,
    NCG_EXPONENTS,
    REFERENCE_CW_SUPPLY_C,
    REFERENCE_LOAD_KW,
    SUBCOOLING_EXCESS,
    SWEEP_CASES,
    SWEEP_SEEDS,
    collapse_bands,
    run_sweeps,
    separation_claims,
    sweep_markdown,
    _signature_distributions,
)
from chiller_sim.separation import interval_gap


@pytest.fixture(scope="module")
def sweep_results():
    return run_sweeps()


def _case_calibration(sweep_results, case: str) -> pd.Series:
    return sweep_results.calibration.set_index("case").loc[case]


def test_noise_free_calibration_and_reference_point_invariance(sweep_results) -> None:
    config = SimulationConfig()
    healthy = solve_operating_point(
        REFERENCE_LOAD_KW,
        REFERENCE_CW_SUPPLY_C,
        config.ua_cond_kw_per_k,
        config,
    )
    fouling = solve_operating_point(
        REFERENCE_LOAD_KW,
        REFERENCE_CW_SUPPLY_C,
        100.0,
        config,
    )
    target_excess = float(
        fouling.discharge_pressure_kpa - healthy.discharge_pressure_kpa
    )
    for case, ua in (("ncg_dalton", 200.0), ("ncg_blanketed", 150.0)):
        calibration = _case_calibration(sweep_results, case)
        base = solve_operating_point(
            REFERENCE_LOAD_KW,
            REFERENCE_CW_SUPPLY_C,
            ua,
            config,
            use_total_pressure=True,
            ncg_partial_pressure_kpa=calibration.ncg_partial_pressure_kpa,
            ncg_reference_pressure_kpa=calibration.ncg_reference_pressure_kpa,
        )
        np.testing.assert_allclose(
            float(base.discharge_pressure_kpa - healthy.discharge_pressure_kpa),
            target_excess,
            atol=0.05,
            rtol=0.0,
        )
        for exponent in NCG_EXPONENTS:
            varied = solve_operating_point(
                REFERENCE_LOAD_KW,
                REFERENCE_CW_SUPPLY_C,
                ua,
                config,
                use_total_pressure=True,
                ncg_partial_pressure_kpa=calibration.ncg_partial_pressure_kpa,
                ncg_pressure_exponent=exponent,
                ncg_reference_pressure_kpa=calibration.ncg_reference_pressure_kpa,
            )
            for attribute in (
                "evaporator_temp_c",
                "condenser_temp_c",
                "compressor_power_kw",
                "chw_return_c",
                "cw_return_c",
                "suction_pressure_kpa",
                "discharge_pressure_kpa",
                "ncg_partial_pressure_kpa",
            ):
                np.testing.assert_allclose(
                    getattr(varied, attribute),
                    getattr(base, attribute),
                    atol=1e-9,
                    rtol=0.0,
                )


def test_n_equals_one_keeps_ncg_mole_fraction_constant(sweep_results) -> None:
    config = SimulationConfig()
    calibration = _case_calibration(sweep_results, "ncg_dalton")
    point = solve_operating_point(
        LOAD_POINTS_KW,
        np.full(len(LOAD_POINTS_KW), REFERENCE_CW_SUPPLY_C),
        200.0,
        config,
        use_total_pressure=True,
        ncg_partial_pressure_kpa=calibration.ncg_partial_pressure_kpa,
        ncg_pressure_exponent=1.0,
        ncg_reference_pressure_kpa=calibration.ncg_reference_pressure_kpa,
    )
    fraction = point.ncg_partial_pressure_kpa / saturation_pressure_kpa(
        point.condenser_temp_c
    )
    assert float(np.ptp(fraction)) <= 1e-9


def test_blanketing_only_is_fouling_by_construction(sweep_results) -> None:
    config = SimulationConfig()
    fouling = solve_operating_point(
        LOAD_POINTS_KW,
        np.full(len(LOAD_POINTS_KW), REFERENCE_CW_SUPPLY_C),
        100.0,
        config,
    )
    blanketing_only = solve_operating_point(
        LOAD_POINTS_KW,
        np.full(len(LOAD_POINTS_KW), REFERENCE_CW_SUPPLY_C),
        100.0,
        config,
        use_total_pressure=True,
        ncg_partial_pressure_kpa=0.0,
    )
    for attribute in (
        "evaporator_temp_c",
        "condenser_temp_c",
        "compressor_power_kw",
        "chw_return_c",
        "cw_return_c",
        "suction_pressure_kpa",
        "discharge_pressure_kpa",
        "ncg_partial_pressure_kpa",
    ):
        np.testing.assert_allclose(
            getattr(blanketing_only, attribute),
            getattr(fouling, attribute),
            atol=1e-9,
            rtol=0.0,
        )
    generated = sweep_markdown(sweep_results)
    for prefix in (
        "Claimed separators (every sweep, every n, every NCG case with nonzero partial pressure): ",
        "Not claimed (collapses or reverses in at least one sweep): ",
    ):
        claim_line = generated.split(prefix, maxsplit=1)[1].split(".", maxsplit=1)[0]
        assert "ncg_blanketing_only" not in claim_line


def _load_claim_frame(
    *,
    overlapping_n: float | None = None,
    reversed_n: float | None = None,
) -> pd.DataFrame:
    rows = []
    for case in NCG_CASES:
        for exponent in NCG_EXPONENTS:
            separable = exponent != overlapping_n
            direction = -1.0 if exponent == reversed_n else 1.0
            rows.append(
                {
                    "signature": LOAD_ELASTICITY,
                    "sweep": "load",
                    "case": case,
                    "n": exponent,
                    "point_index": np.nan,
                    "separable": separable,
                    "direction": direction,
                }
            )
    return pd.DataFrame(rows)


def test_claims_reject_overlap_at_any_exponent() -> None:
    claims = separation_claims(_load_claim_frame(overlapping_n=2.0))
    row = claims.set_index("signature").loc[LOAD_ELASTICITY]
    assert not bool(row["claimed"])
    assert 2.0 in collapse_bands(
        _load_claim_frame(overlapping_n=2.0)
    )[(LOAD_ELASTICITY, "load", "ncg_dalton")]


def test_claims_reject_direction_flip_between_disjoint_exponents() -> None:
    claims = separation_claims(_load_claim_frame(reversed_n=2.0))
    row = claims.set_index("signature").loc[LOAD_ELASTICITY]
    assert not bool(row["claimed"])
    assert 2.0 in collapse_bands(
        _load_claim_frame(reversed_n=2.0)
    )[(LOAD_ELASTICITY, "load", "ncg_dalton")]


def test_claims_accept_universally_disjoint_consistent_direction() -> None:
    claims = separation_claims(_load_claim_frame())
    row = claims.set_index("signature").loc[LOAD_ELASTICITY]
    assert bool(row["claimed"])
    assert collapse_bands(_load_claim_frame())[
        (LOAD_ELASTICITY, "load", "ncg_dalton")
    ] == ()


def test_nonpositive_approach_excess_produces_nan_elasticity() -> None:
    points = pd.DataFrame(
        {
            "sweep": ["load", "load"],
            "case": ["ncg_dalton", "ncg_dalton"],
            "n": [0.0, 0.0],
            "seed": [0, 0],
            "approach_excess_c": [-0.1, 0.2],
            "condenser_heat_kw": [400.0, 500.0],
        }
    )
    signature = _signature_distributions(points)
    assert np.isnan(signature.iloc[0]["value"])


def test_committed_sweep_summary_matches_recomputation(sweep_results) -> None:
    root = Path(__file__).resolve().parents[1]
    committed = pd.read_csv(root / "reports/sweep_separation_summary.csv")
    pd.testing.assert_frame_equal(
        committed,
        sweep_results.separation,
        check_dtype=False,
        check_exact=False,
        atol=1e-9,
        rtol=1e-9,
    )


def test_separation_rows_match_raw_seed_distributions(sweep_results) -> None:
    for row in sweep_results.separation.itertuples(index=False):
        if row.signature == SUBCOOLING_EXCESS:
            case_values = sweep_results.point_metrics.loc[
                (sweep_results.point_metrics["sweep"] == row.sweep)
                & (sweep_results.point_metrics["case"] == row.case)
                & (sweep_results.point_metrics["n"] == row.n)
                & (sweep_results.point_metrics["point_index"] == row.point_index),
                "apparent_subcooling_excess_c",
            ].to_numpy(dtype=float)
            reference_values = sweep_results.point_metrics.loc[
                (sweep_results.point_metrics["sweep"] == row.sweep)
                & (
                    sweep_results.point_metrics["case"]
                    == "condenser_fouling"
                )
                & (
                    sweep_results.point_metrics["point_index"]
                    == row.point_index
                ),
                "apparent_subcooling_excess_c",
            ].to_numpy(dtype=float)
        else:
            case_values = sweep_results.signatures.loc[
                (sweep_results.signatures["signature"] == row.signature)
                & (sweep_results.signatures["sweep"] == row.sweep)
                & (sweep_results.signatures["case"] == row.case)
                & (sweep_results.signatures["n"] == row.n),
                "value",
            ].to_numpy(dtype=float)
            reference_values = sweep_results.signatures.loc[
                (sweep_results.signatures["signature"] == row.signature)
                & (sweep_results.signatures["sweep"] == row.sweep)
                & (
                    sweep_results.signatures["case"]
                    == "condenser_fouling"
                ),
                "value",
            ].to_numpy(dtype=float)
        if np.isfinite(case_values).all() and np.isfinite(
            reference_values
        ).all():
            assert row.ref_min == pytest.approx(reference_values.min())
            assert row.ref_max == pytest.approx(reference_values.max())
            assert row.case_min == pytest.approx(case_values.min())
            assert row.case_max == pytest.approx(case_values.max())
            assert row.gap == pytest.approx(
                interval_gap(reference_values, case_values)
            )
            assert row.separable == (row.gap > 0.0)
        else:
            assert not row.separable


def test_generated_report_block_and_artifacts_match(sweep_results) -> None:
    root = Path(__file__).resolve().parents[1]
    report = (root / "diagnostic_report.md").read_text()
    assert "## Third analysis: operating-point sweeps (does the approach-vs-load separator survive?)" in report
    assert "### Stated before implementation" in report
    assert "### Prediction vs outcome" in report
    assert "### What still separates fouling from NCG, and what does not" in report
    generated = report.split(BEGIN_MARKER, maxsplit=1)[1].split(
        "<!-- END GENERATED: sweep-separation -->", maxsplit=1
    )[0].strip()
    assert generated == sweep_markdown(sweep_results)
    claim_line = generated.split(
        "Claimed separators (every sweep, every n, every NCG case with nonzero partial pressure): ",
        maxsplit=1,
    )[1].split(".", maxsplit=1)[0]
    if not sweep_results.claims.set_index("signature").loc[
        LOAD_ELASTICITY, "claimed"
    ]:
        assert LOAD_ELASTICITY not in claim_line
    for filename in (
        "sweep_load_signature.png",
        "sweep_cw_inlet_signature.png",
        "sweep_ncg_exponent.png",
    ):
        assert (root / "reports" / filename).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_approach_elasticity_claim_guard(sweep_results) -> None:
    rows = sweep_results.separation.loc[
        (sweep_results.separation["signature"] == LOAD_ELASTICITY)
        & sweep_results.separation["case"].isin(NCG_CASES)
    ]
    should_claim = True
    for case in NCG_CASES:
        case_rows = rows.loc[rows["case"] == case]
        reference_direction = case_rows.loc[
            case_rows["n"] == 0.0, "direction"
        ].iloc[0]
        if not case_rows["separable"].astype(bool).all():
            should_claim = False
        for row in case_rows.itertuples(index=False):
            if row.direction != reference_direction:
                should_claim = False
    claimed = bool(
        sweep_results.claims.set_index("signature").loc[
            LOAD_ELASTICITY, "claimed"
        ]
    )
    assert claimed == should_claim
    if not should_claim:
        report_claim = sweep_markdown(sweep_results).split(
            "Claimed separators (every sweep, every n, every NCG case with nonzero partial pressure): ",
            maxsplit=1,
        )[1].split(".", maxsplit=1)[0]
        assert LOAD_ELASTICITY not in report_claim


def test_operating_point_broadcasting_and_reference_validation() -> None:
    config = SimulationConfig()
    point = solve_operating_point(
        np.array([500.0, 650.0]),
        REFERENCE_CW_SUPPLY_C,
        100.0,
        config,
    )
    assert point.condenser_temp_c.shape == (2,)
    assert len(SWEEP_SEEDS) == 20
    assert len(NCG_EXPONENTS) == 21
    assert len(CW_POINTS_C) == 11
    assert LOAD_POINTS_KW.shape == (14,)
    assert len(SWEEP_CASES) == len(CASE_INDEX)
    with pytest.raises(ValueError, match="ncg_reference_pressure_kpa"):
        solve_operating_point(
            650.0,
            28.0,
            200.0,
            config,
            use_total_pressure=True,
            ncg_partial_pressure_kpa=10.0,
            ncg_pressure_exponent=1.0,
        )


def test_subcooling_rows_require_every_point_for_separation(sweep_results) -> None:
    rows = sweep_results.separation.loc[
        (sweep_results.separation["signature"] == SUBCOOLING_EXCESS)
        & (sweep_results.separation["case"] == "ncg_dalton")
        & (sweep_results.separation["sweep"] == "load")
        & (sweep_results.separation["n"] == 0.0)
    ]
    assert len(rows) == len(LOAD_POINTS_KW)
    assert rows["separable"].astype(bool).all() == rows["gap"].gt(0.0).all()
