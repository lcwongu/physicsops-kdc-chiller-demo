import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd

from chiller_sim.diagnostics import (
    Diagnosis,
    compare_periods,
    derive_signals,
    diagnose,
    measured_changes_markdown,
    trend_slopes,
)
from chiller_sim.model import SimulationConfig


BEGIN_MARKER = "<!-- BEGIN GENERATED: measured-changes -->"
END_MARKER = "<!-- END GENERATED: measured-changes -->"


def _validated_report_text(path: Path) -> str:
    text = path.read_text()
    if text.count(BEGIN_MARKER) != 1 or text.count(END_MARKER) != 1:
        raise ValueError("diagnostic report must contain each generated-block marker once")
    start = text.index(BEGIN_MARKER) + len(BEGIN_MARKER)
    end = text.index(END_MARKER)
    if end < start:
        raise ValueError("diagnostic report generated-block markers are out of order")
    return text


def _timeseries_plot(
    df: pd.DataFrame,
    output_path: Path,
    config: SimulationConfig,
) -> None:
    sensors = derive_signals(df, config)
    hourly = (
        sensors.set_index("timestamp")
        .resample("1h")
        .mean(numeric_only=True)
    )
    fault_start = pd.Timestamp(config.fault_start)
    fig, axes = plt.subplots(5, 1, figsize=(12, 15), sharex=True)

    axes[0].plot(
        hourly.index,
        hourly["ua_cond_est_kw_per_k"],
        label="sensor-derived UA estimate",
    )
    if "condenser_ua_kw_per_k" in df:
        truth = (
            df.set_index("timestamp")["condenser_ua_kw_per_k"]
            .resample("1h")
            .mean()
        )
        axes[0].plot(
            truth.index,
            truth,
            linestyle="--",
            label="ground truth, validation only",
        )
    axes[0].set_ylabel("UA (kW/K)")
    axes[0].legend()

    axes[1].plot(hourly.index, hourly["cw_supply_temp_c"], label="CW supply")
    axes[1].plot(hourly.index, hourly["cw_return_temp_c"], label="CW return")
    axes[1].plot(hourly.index, hourly["condenser_approach_c"], label="approach")
    axes[1].set_ylabel("Temperature (°C)")
    axes[1].legend()

    axes[2].plot(hourly.index, hourly["discharge_pressure_kpa"])
    axes[2].set_ylabel("Discharge (kPa)")

    axes[3].plot(hourly.index, hourly["compressor_power_kw"])
    axes[3].set_ylabel("Power (kW)")

    axes[4].plot(hourly.index, hourly["cop"])
    axes[4].set_ylabel("COP")
    axes[4].set_xlabel("Timestamp")

    for axis in axes:
        axis.axvline(fault_start, color="black", linestyle=":", linewidth=1)
        axis.grid(True, alpha=0.25)
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def _hourly_profiles_plot(
    df: pd.DataFrame,
    output_path: Path,
    config: SimulationConfig,
    degraded_hours: int = 24,
) -> None:
    sensors = df.drop(
        columns=["fault_severity", "condenser_ua_kw_per_k", "fault_active"],
        errors="ignore",
    ).copy()
    timestamps = pd.to_datetime(sensors["timestamp"])
    healthy = timestamps < pd.Timestamp(config.fault_start)
    degraded = timestamps >= timestamps.max() - pd.Timedelta(hours=degraded_hours)
    signals = (
        ("discharge_pressure_kpa", "Discharge pressure (kPa)"),
        ("compressor_power_kw", "Compressor power (kW)"),
        ("cop", "COP"),
    )
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.5))
    for axis, (signal, title) in zip(axes, signals):
        healthy_profile = sensors.loc[healthy].groupby(
            timestamps[healthy].dt.hour
        )[signal].mean()
        degraded_profile = sensors.loc[degraded].groupby(
            timestamps[degraded].dt.hour
        )[signal].mean()
        axis.plot(healthy_profile.index, healthy_profile, label="healthy")
        axis.plot(degraded_profile.index, degraded_profile, label="degraded")
        axis.set_title(title)
        axis.set_xlabel("Hour of day")
        axis.set_xticks(range(0, 24, 4))
        axis.grid(True, alpha=0.25)
        axis.legend()
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def _changes_plot(comparison: pd.DataFrame, output_path: Path) -> None:
    values = comparison["pct_change"]
    fig, axis = plt.subplots(figsize=(9, 6))
    axis.barh(values.index[::-1], values.to_numpy()[::-1])
    axis.axvline(0.0, color="black", linewidth=0.8)
    axis.set_xlabel("Change (%)")
    axis.set_title("Hour-matched degraded vs. healthy changes")
    axis.grid(axis="x", alpha=0.25)
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def generate_report(
    df: pd.DataFrame,
    output_dir: str | Path = "reports",
    report_path: str | Path = "diagnostic_report.md",
) -> Diagnosis:
    output_directory = Path(output_dir)
    markdown_path = Path(report_path)
    markdown = _validated_report_text(markdown_path)
    output_directory.mkdir(parents=True, exist_ok=True)

    comparison = compare_periods(df)
    trends = trend_slopes(df)
    diagnosis = diagnose(comparison)
    summary = comparison.join(trends)
    summary.to_csv(output_directory / "diagnostic_summary.csv", index_label="signal")
    _timeseries_plot(
        df,
        output_directory / "diagnostic_timeseries.png",
        SimulationConfig(),
    )
    _hourly_profiles_plot(
        df,
        output_directory / "diagnostic_hourly_profiles.png",
        SimulationConfig(),
    )
    _changes_plot(comparison, output_directory / "diagnostic_changes.png")

    table = measured_changes_markdown(comparison, trends)
    start = markdown.index(BEGIN_MARKER) + len(BEGIN_MARKER)
    end = markdown.index(END_MARKER)
    updated = markdown[:start] + "\n\n" + table + "\n\n" + markdown[end:]
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(updated)
    return diagnosis


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the chiller diagnostic report")
    parser.add_argument("--input", default="data/chiller_telemetry.csv")
    parser.add_argument("--output-dir", default="reports")
    parser.add_argument("--report", default="diagnostic_report.md")
    args = parser.parse_args()
    telemetry = pd.read_csv(args.input, parse_dates=["timestamp"])
    generate_report(telemetry, args.output_dir, args.report)


if __name__ == "__main__":
    main()
