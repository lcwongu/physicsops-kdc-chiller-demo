import argparse
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from chiller_sim.diagnostics import (
    COMPARISON_SIGNALS,
    compare_periods,
    derive_signals,
    diagnose,
)
from chiller_sim.model import (
    NCG_PARTIAL_PRESSURE_P_B_KPA,
    SimulationConfig,
    generate_telemetry,
)


FAULT_CASES = {
    "condenser_fouling": {},
    "ncg_dalton": {"fault_type": "non_condensables"},
    "ncg_blanketed": {
        "fault_type": "non_condensables",
        "ncg_max_partial_pressure_kpa": NCG_PARTIAL_PRESSURE_P_B_KPA,
        "ncg_blanketing_ua_loss": 0.25,
    },
}
REFERENCE_CASE = "condenser_fouling"
SEPARATION_SEEDS = tuple(range(20))
SEPARATION_METRICS = (
    ("discharge_pressure_delta_kpa", "current sensors"),
    ("compressor_power_pct", "current sensors"),
    ("cop_pct", "current sensors"),
    ("ua_cond_est_pct", "current sensors"),
    ("condenser_approach_delta_c", "current sensors"),
    ("cw_range_delta_c", "current sensors"),
    ("approach_load_slope_change_k_per_100kw", "current sensors"),
    ("apparent_subcooling_delta_c", "added liquid-temperature sensor"),
)

BEGIN_MARKER = "<!-- BEGIN GENERATED: fault-separation -->"
END_MARKER = "<!-- END GENERATED: fault-separation -->"


def _case_config(case: str, seed: int) -> SimulationConfig:
    return SimulationConfig(seed=seed, **FAULT_CASES[case])


def case_metrics(df: pd.DataFrame, config: SimulationConfig) -> dict[str, float]:
    signals = (*COMPARISON_SIGNALS, "apparent_subcooling_c")
    comparison = compare_periods(df, config, signals=signals)

    hourly = (
        derive_signals(df, config)
        .set_index("timestamp")
        .resample("1h")
        .mean(numeric_only=True)
    )
    healthy = hourly.index < pd.Timestamp(config.fault_start)
    degraded = hourly.index >= hourly.index.max() - pd.Timedelta(hours=24)

    def load_slope(mask: pd.Series) -> float:
        window = hourly.loc[mask]
        elapsed_hours = (
            (window.index - window.index[0]).total_seconds().to_numpy() / 3600.0
        )
        design = np.column_stack(
            [
                np.ones(len(window)),
                window["condenser_heat_kw"].to_numpy() / 100.0,
                elapsed_hours,
            ]
        )
        coefficients, *_ = np.linalg.lstsq(
            design, window["condenser_approach_c"].to_numpy(), rcond=None
        )
        return float(coefficients[1])

    approach_slope_change = load_slope(degraded) - load_slope(healthy)
    return {
        "discharge_pressure_delta_kpa": float(
            comparison.loc["discharge_pressure_kpa", "delta"]
        ),
        "compressor_power_pct": float(
            comparison.loc["compressor_power_kw", "pct_change"]
        ),
        "cop_pct": float(comparison.loc["cop", "pct_change"]),
        "ua_cond_est_pct": float(
            comparison.loc["ua_cond_est_kw_per_k", "pct_change"]
        ),
        "condenser_approach_delta_c": float(
            comparison.loc["condenser_approach_c", "delta"]
        ),
        "cw_range_delta_c": float(comparison.loc["cw_range_c", "delta"]),
        "approach_load_slope_change_k_per_100kw": approach_slope_change,
        "apparent_subcooling_delta_c": float(
            comparison.loc["apparent_subcooling_c", "delta"]
        ),
    }


def metric_distributions(
    seeds: tuple[int, ...] = SEPARATION_SEEDS,
) -> pd.DataFrame:
    rows = []
    for seed in seeds:
        for case in FAULT_CASES:
            config = _case_config(case, seed)
            metrics = case_metrics(generate_telemetry(config), config)
            rows.append({"case": case, "seed": seed, **metrics})
    return pd.DataFrame(rows, columns=["case", "seed", *[m for m, _ in SEPARATION_METRICS]])


def interval_gap(a: np.ndarray, b: np.ndarray) -> float:
    first = np.asarray(a, dtype=float)
    second = np.asarray(b, dtype=float)
    return float(max(first.min() - second.max(), second.min() - first.max()))


def separability(dist: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for metric, source in SEPARATION_METRICS:
        reference = dist.loc[dist["case"] == REFERENCE_CASE, metric].to_numpy()
        for case in FAULT_CASES:
            if case == REFERENCE_CASE:
                continue
            values = dist.loc[dist["case"] == case, metric].to_numpy()
            gap = interval_gap(reference, values)
            rows.append(
                {
                    "metric": metric,
                    "source": source,
                    "case": case,
                    "ref_min": float(reference.min()),
                    "ref_max": float(reference.max()),
                    "case_min": float(values.min()),
                    "case_max": float(values.max()),
                    "gap": gap,
                    "separable": gap > 0.0,
                }
            )
    return pd.DataFrame(
        rows,
        columns=[
            "metric",
            "source",
            "case",
            "ref_min",
            "ref_max",
            "case_min",
            "case_max",
            "gap",
            "separable",
        ],
    )


def separation_claims(sep: pd.DataFrame) -> pd.DataFrame:
    ncg_cases = tuple(case for case in FAULT_CASES if case != REFERENCE_CASE)
    rows = []
    for metric, source in SEPARATION_METRICS:
        metric_rows = sep.loc[sep["metric"] == metric].set_index("case")
        all_variants = set(ncg_cases).issubset(metric_rows.index)
        claimed = all_variants and bool(
            metric_rows.loc[list(ncg_cases), "separable"].astype(bool).all()
        )
        rows.append(
            {
                "metric": metric,
                "source": source,
                "separable_vs_all_ncg_variants": claimed,
            }
        )
    return pd.DataFrame(
        rows,
        columns=["metric", "source", "separable_vs_all_ncg_variants"],
    )


def _format_three(value: float) -> str:
    rounded = Decimal(str(float(value))).quantize(
        Decimal("0.001"), rounding=ROUND_HALF_UP
    )
    return f"{rounded:.3f}"


def _seed42_metrics() -> dict[str, dict[str, float]]:
    results = {}
    for case in FAULT_CASES:
        config = _case_config(case, 42)
        results[case] = case_metrics(generate_telemetry(config), config)
    return results


def separation_markdown(
    dist: pd.DataFrame,
    sep: pd.DataFrame,
    claims: pd.DataFrame,
    seed42_diagnoses: dict[str, str],
) -> str:
    seed42_metrics = _seed42_metrics()
    calibration_rows = [
        "| case | max NCG partial pressure (kPa) | blanketing UA loss | "
        "discharge pressure Δ (kPa) | diagnose() mechanism |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for case, overrides in FAULT_CASES.items():
        config = SimulationConfig(seed=42, **overrides)
        ncg_pressure = (
            config.ncg_max_partial_pressure_kpa
            if case != REFERENCE_CASE
            else 0.0
        )
        calibration_rows.append(
            "| "
            + " | ".join(
                (
                    case,
                    _format_three(ncg_pressure),
                    _format_three(config.ncg_blanketing_ua_loss),
                    _format_three(
                        seed42_metrics[case]["discharge_pressure_delta_kpa"]
                    ),
                    seed42_diagnoses[case],
                )
            )
            + " |"
        )

    range_rows = [
        "| metric | source | fouling range | ncg_dalton range | gap | separable | "
        "ncg_blanketed range | gap | separable |",
        "| --- | --- | ---: | ---: | ---: | --- | ---: | ---: | --- |",
    ]
    sep_by_key = sep.set_index(["metric", "case"])
    for metric, source in SEPARATION_METRICS:
        reference = dist.loc[dist["case"] == REFERENCE_CASE, metric]
        cells = [
            metric,
            source,
            f"{_format_three(reference.min())}–{_format_three(reference.max())}",
        ]
        for case in ("ncg_dalton", "ncg_blanketed"):
            values = dist.loc[dist["case"] == case, metric]
            row = sep_by_key.loc[(metric, case)]
            cells.extend(
                [
                    f"{_format_three(values.min())}–{_format_three(values.max())}",
                    _format_three(row["gap"]),
                    "yes" if bool(row["separable"]) else "no",
                ]
            )
        range_rows.append("| " + " | ".join(cells) + " |")

    claimed = claims.loc[
        claims["separable_vs_all_ncg_variants"], "metric"
    ].tolist()
    not_robust = claims.loc[
        ~claims["separable_vs_all_ncg_variants"], "metric"
    ].tolist()
    claimed_text = ", ".join(claimed) if claimed else "none"
    not_robust_text = ", ".join(not_robust) if not_robust else "none"

    lines = [
        "### Calibration and seed-42 comparison",
        "",
        *calibration_rows,
        "",
        "### Seed-sweep ranges and interval gaps",
        "",
        *range_rows,
        "",
        f"Claimed separators (disjoint from fouling for every NCG variant): {claimed_text}.",
        f"Overlapping or not robust: {not_robust_text}.",
        "",
        "Seed-42 `diagnose()` mechanisms:",
    ]
    lines.extend(
        f"- {case}: {seed42_diagnoses[case]}" for case in FAULT_CASES
    )
    return "\n".join(lines)


def _validate_report_text(path: Path) -> str:
    text = path.read_text()
    if text.count(BEGIN_MARKER) != 1 or text.count(END_MARKER) != 1:
        raise ValueError(
            "diagnostic report must contain each fault-separation marker once"
        )
    start = text.index(BEGIN_MARKER) + len(BEGIN_MARKER)
    end = text.index(END_MARKER)
    if end < start:
        raise ValueError("fault-separation markers are out of order")
    return text


def _seed42_cases() -> dict[str, tuple[SimulationConfig, pd.DataFrame]]:
    results = {}
    for case in FAULT_CASES:
        config = _case_config(case, 42)
        results[case] = (config, generate_telemetry(config))
    return results


def _timeseries_plot(
    cases: dict[str, tuple[SimulationConfig, pd.DataFrame]],
    output_path: Path,
) -> None:
    metrics = (
        ("discharge_pressure_kpa", "Discharge pressure (kPa)"),
        ("compressor_power_kw", "Compressor power (kW)"),
        ("cop", "COP"),
        ("ua_cond_est_kw_per_k", "Sensor UA estimate (kW/K)"),
        ("condenser_approach_c", "Condenser approach (°C)"),
        ("apparent_subcooling_c", "Apparent subcooling (°C)"),
    )
    fig, axes = plt.subplots(6, 1, figsize=(13, 18), sharex=True)
    fault_start = pd.Timestamp(SimulationConfig().fault_start)
    for case, (config, df) in cases.items():
        hourly = (
            derive_signals(df, config)
            .set_index("timestamp")
            .resample("1h")
            .mean(numeric_only=True)
        )
        for axis, (signal, title) in zip(axes, metrics):
            axis.plot(hourly.index, hourly[signal], label=case)
            axis.set_ylabel(title)
    for axis in axes:
        axis.axvline(fault_start, color="black", linestyle=":", linewidth=1)
        axis.grid(True, alpha=0.25)
        axis.legend()
    axes[-1].set_xlabel("Timestamp")
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def _ranges_plot(
    dist: pd.DataFrame,
    claims: pd.DataFrame,
    output_path: Path,
) -> None:
    fig, axes = plt.subplots(
        len(SEPARATION_METRICS), 1, figsize=(12, 3.1 * len(SEPARATION_METRICS))
    )
    colors = {
        "condenser_fouling": "tab:blue",
        "ncg_dalton": "tab:orange",
        "ncg_blanketed": "tab:green",
    }
    claimed = claims.set_index("metric")["separable_vs_all_ncg_variants"]
    rng = np.random.default_rng(42)
    for axis, (metric, _) in zip(axes, SEPARATION_METRICS):
        reference = dist.loc[dist["case"] == REFERENCE_CASE, metric]
        axis.axhspan(reference.min(), reference.max(), color="tab:blue", alpha=0.12)
        for position, case in enumerate(FAULT_CASES):
            values = dist.loc[dist["case"] == case, metric].to_numpy()
            jitter = rng.uniform(-0.08, 0.08, size=len(values))
            axis.scatter(
                position + jitter,
                values,
                color=colors[case],
                label=case,
                alpha=0.8,
                s=24,
            )
        axis.set_xticks(range(len(FAULT_CASES)), list(FAULT_CASES))
        verdict = "separable" if bool(claimed.loc[metric]) else "overlap"
        axis.set_title(f"{metric}: {verdict}")
        axis.grid(axis="y", alpha=0.25)
    axes[0].legend(ncol=3)
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def _load_signature_plot(
    cases: dict[str, tuple[SimulationConfig, pd.DataFrame]],
    output_path: Path,
) -> None:
    fig, axis = plt.subplots(figsize=(10, 6))
    colors = {
        "condenser_fouling": "tab:blue",
        "ncg_dalton": "tab:orange",
        "ncg_blanketed": "tab:green",
    }
    for case, (config, df) in cases.items():
        hourly = (
            derive_signals(df, config)
            .set_index("timestamp")
            .resample("1h")
            .mean(numeric_only=True)
        )
        healthy = hourly.index < pd.Timestamp(config.fault_start)
        degraded = hourly.index >= hourly.index.max() - pd.Timedelta(hours=24)
        for name, mask in (("healthy", healthy), ("final 24 h", degraded)):
            window = hourly.loc[mask]
            elapsed_hours = (
                (window.index - window.index[0]).total_seconds().to_numpy()
                / 3600.0
            )
            load = window["condenser_heat_kw"].to_numpy() / 100.0
            design = np.column_stack(
                [np.ones(len(window)), load, elapsed_hours]
            )
            coefficients, *_ = np.linalg.lstsq(
                design, window["condenser_approach_c"].to_numpy(), rcond=None
            )
            axis.scatter(
                window["condenser_heat_kw"],
                window["condenser_approach_c"],
                color=colors[case],
                marker="o" if name == "healthy" else "x",
                alpha=0.5,
                s=22,
                label=f"{case} {name}",
            )
            x = np.linspace(
                window["condenser_heat_kw"].min(),
                window["condenser_heat_kw"].max(),
                100,
            )
            elapsed_mean = float(elapsed_hours.mean())
            y = (
                coefficients[0]
                + coefficients[1] * x / 100.0
                + coefficients[2] * elapsed_mean
            )
            axis.plot(x, y, color=colors[case], linestyle="-" if name == "healthy" else "--")
    axis.set_xlabel("Condenser heat (kW)")
    axis.set_ylabel("Condenser approach (°C)")
    axis.grid(True, alpha=0.25)
    axis.legend(ncol=2, fontsize="small")
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def generate_separation_report(
    output_dir: str | Path = "reports",
    report_path: str | Path = "diagnostic_report.md",
) -> pd.DataFrame:
    output_directory = Path(output_dir)
    markdown_path = Path(report_path)
    markdown = _validate_report_text(markdown_path)
    output_directory.mkdir(parents=True, exist_ok=True)

    dist = metric_distributions()
    sep = separability(dist)
    claims = separation_claims(sep)
    dist.to_csv(output_directory / "fault_separation_metrics.csv", index=False)
    sep.to_csv(output_directory / "fault_separation_summary.csv", index=False)

    seed42_cases = _seed42_cases()
    seed42_diagnoses = {
        case: diagnose(compare_periods(df, config)).mechanism
        for case, (config, df) in seed42_cases.items()
    }
    _timeseries_plot(
        seed42_cases, output_directory / "fault_comparison_timeseries.png"
    )
    _ranges_plot(
        dist, claims, output_directory / "fault_separation_ranges.png"
    )
    _load_signature_plot(
        seed42_cases, output_directory / "fault_load_signature.png"
    )

    generated = separation_markdown(dist, sep, claims, seed42_diagnoses)
    start = markdown.index(BEGIN_MARKER) + len(BEGIN_MARKER)
    end = markdown.index(END_MARKER)
    markdown_path.write_text(
        markdown[:start] + "\n\n" + generated + "\n\n" + markdown[end:]
    )
    return sep


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare condenser-fouling and non-condensable faults"
    )
    parser.add_argument("--output-dir", default="reports")
    parser.add_argument("--report", default="diagnostic_report.md")
    args = parser.parse_args()
    generate_separation_report(args.output_dir, args.report)


if __name__ == "__main__":
    main()
