import argparse

from chiller_sim.model import SimulationConfig, generate_telemetry, save_telemetry


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate synthetic chiller telemetry")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", default="data/chiller_telemetry.csv")
    args = parser.parse_args()

    telemetry = generate_telemetry(SimulationConfig(seed=args.seed))
    save_telemetry(telemetry, args.output)


if __name__ == "__main__":
    main()
