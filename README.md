# Synthetic Chiller Telemetry

This Python-only project generates reproducible, five-minute chiller telemetry
for a seven-day period. It simulates a water-cooled chiller with either gradual
condenser fouling or non-condensable gas in the condenser. The generator uses
NumPy and pandas; pytest exercises its API, schedule, physics, and committed
CSVs.

## Generate and test

From the repository root:

```bash
pip install -r requirements.txt
python -m chiller_sim --seed 42 --output data/chiller_telemetry.csv
python -m chiller_sim --seed 42 --fault non_condensables --output data/chiller_telemetry_ncg.csv
python -m chiller_sim.sweeps
python -m pytest -q
```

## Diagnostics

Install matplotlib with `pip install -r requirements.txt`, then run
`python -m chiller_sim.report` to create `diagnostic_report.md`, the hourly
diagnostic plots, and `reports/diagnostic_summary.csv`.

The CLI defaults to seed `42`, fault `condenser_fouling`, and
`data/chiller_telemetry.csv`. Select `--fault non_condensables` for the NCG
case. The added condenser-liquid-temperature sensor uses its own
`numpy.random.default_rng([seed, 1])`, so its AR(1) subcooling innovations and
sensor-noise draws do not change the original sensor RNG stream. Original
sensor generation uses one `numpy.random.default_rng(seed)`. Random draws are
deterministic and ordered as follows: one timestamp-ordered matrix of standard-normal
innovations, with cooling-load values then wet-bulb values in each row;
measurement noise for wet bulb, chilled-water supply and return temperatures,
condenser-water supply and return temperatures, chilled-water and
condenser-water flows, compressor power, suction pressure, and discharge
pressure. The first matrix row supplies the AR(1) initial values and remaining
rows supply their innovations. The CSV is saved without a DataFrame index and
formats floating-point values to four decimal places.

## Sampling and assumptions

The default local, timezone-naive interval begins at `2025-07-01 00:00:00`,
lasts seven days, and samples every five minutes. It contains 2,016 rows, from
`2025-07-01 00:00` through `2025-07-07 23:55`.

The model treats chilled-water and condenser-water mass flows as constant
before sensor noise, holds chilled-water supply at its setpoint, and uses a
constant cooling-tower approach. It assumes water specific heat of
`4.186 kJ/(kg·K)`, idealized ε-NTU heat exchangers, a fractional-Carnot
compressor efficiency curve, and an R-134a saturation-pressure fit. No
transient equipment dynamics beyond the two exogenous AR(1) disturbances and
the specified fault ramp are represented. Temperatures are in °C; absolute
temperatures in the compressor equation use °C + `273.15`.

## Model equations and constants

Let `h` be fractional hour of day and `cp = 4.186 kJ/(kg·K)`.

**Exogenous conditions**

```text
Twb = 24 + 3 cos(2π(h - 15)/24) + AR1(phi=0.98, stationary_sd=0.3 °C)
Q_evap = clip(650 + 200 cos(2π(h - 15)/24)
              + AR1(phi=0.95, stationary_sd=25 kW), 200, 950) kW
PLR = Q_evap / 1000 kW
T_cws = Twb + 4.0 °C
```

For either AR(1), the first value is sampled from `N(0, sd)` and each
subsequent value is `x[k] = phi*x[k-1] + sqrt(1 - phi²)*sd*N(0, 1)`.

**Chiller and condenser fouling**

```text
m_chw = 40 kg/s                    m_cw = 50 kg/s
T_chws = 7.0 °C                    UA_e = 150 kW/K
UA_c,healthy = 200 kW/K            rated capacity = 1000 kW

s(t) = 0                                           if t < t_fault
       0.5 * clip((t - t_fault)/(t_last - t_fault), 0, 1) otherwise
UA_c(t) = 200 * (1 - s(t)) kW/K

T_chwr = T_chws + Q_evap/(m_chw*cp)
ε_e = 1 - exp(-UA_e/(m_chw*cp))
T_evap = T_chwr - Q_evap/(ε_e*m_chw*cp)

ε_c = 1 - exp(-UA_c/(m_cw*cp))
Q_cond = Q_evap + W
T_cond = T_cws + Q_cond/(ε_c*m_cw*cp)
η(PLR) = 0.55 * (1 - 0.3*(PLR - 0.8)²)
COP_true = η * (T_evap + 273.15)/(T_cond - T_evap)
W = Q_evap/COP_true
T_cwr = T_cws + Q_cond/(m_cw*cp)
```

`W` and `T_cond` are coupled. The generator starts with `W = Q_evap/5` and
performs vectorized fixed-point updates for at most 50 iterations. It stops
when the maximum absolute power update is below `1e-9 kW`; otherwise generation
raises `RuntimeError`.

**Non-condensables in the condenser**

```text
progress(t) = clip((t - t_fault)/(t_last - t_fault), 0, 1)
p_ncg = p_ncg,max * progress
UA_c = 200 * (1 - blanketing_loss * progress) kW/K
P_discharge = P_sat(T_cond) + p_ncg
T_lift = T_sat(P_discharge)
COP_true = η * (T_evap + 273.15)/(T_lift - T_evap)
```

The model applies Dalton's law: NCG partial pressure adds to the refrigerant
saturation pressure at the condenser heat-transfer temperature. The optional
blanketing variant also reduces condenser UA. Fouling remains a separate,
unchanged fault path.

**Refrigerant pressures and measurements**

The saturation-pressure approximation, in kPa absolute, is
`P_sat(T) = exp(15.425 - 2662/(T + 273.15))`, with `T` in °C. It fits
`292.8 kPa` at `0 °C` and `1016.6 kPa` at `40 °C`, and is within about 1% of
tables over `0–50 °C`. Suction pressure is `P_sat(T_evap)`. For fouling,
discharge pressure is `P_sat(T_cond)`; for NCG it includes the partial pressure
shown above.

Independent Gaussian sensor noise is added to the true values: `0.05 °C` to
each of the six temperature sensors (four water temperatures, wet bulb, and
condenser liquid temperature), `0.3%` of nominal flow to each flow sensor,
`1.0 kW` to compressor power, and `2.0 kPa` to each pressure sensor. The liquid
sensor uses modeled subcooling
`clip(2 + 1*(PLR - 0.65) + AR1(phi=0.95, sd=0.3 °C), 0.2, ∞)`, independent of
the fault, followed by the same `0.05 °C` sensor noise. Derived BMS values use
measured sensors:

```text
cooling_load_kw = chw_flow_kg_s * cp *
                  (chw_return_temp_c - chw_supply_temp_c)
cop = cooling_load_kw / compressor_power_kw
```

## Fault schedule and labels

The fault starts at `2025-07-04 00:00`. Condenser UA degrades linearly from
healthy operation to a 50% loss at the final sample (`fault_severity = 0.5`,
`condenser_ua_kw_per_k = 100`). For NCG, `fault_severity` is the unit progress
from 0 to 1. Discharge-pressure-matched NCG cases reach `89.2 kPa` (Dalton) and
`58.8 kPa` (25% blanketing) at full progress. UA-estimate-matched cases reach
`82.1 kPa` (Dalton) and `52.9 kPa` (25% blanketing). The former match the
fouling case's seed-42 final-24-hour discharge-pressure rise; the latter match
its sensor-estimated UA percentage change. Severity/progress is zero before
the fault start; `fault_active` switches to `1` at and after that time.

Real condenser fouling usually builds over weeks to months. This demo
compresses the ramp into four days so the fault is visible in a one-week
dataset.

`fault_severity`, `condenser_ua_kw_per_k`, `ncg_partial_pressure_kpa`, and
`fault_active` are ground-truth simulation labels, not sensor measurements.

## Output columns

| Column | Unit | Meaning |
|---|---:|---|
| `timestamp` | local datetime | Naive sample timestamp |
| `outdoor_wet_bulb_c` | °C | Measured outdoor wet-bulb temperature |
| `chw_supply_temp_c` | °C | Measured chilled-water supply temperature |
| `chw_return_temp_c` | °C | Measured chilled-water return temperature |
| `cw_supply_temp_c` | °C | Measured condenser-water supply temperature |
| `cw_return_temp_c` | °C | Measured condenser-water return temperature |
| `chw_flow_kg_s` | kg/s | Measured chilled-water mass flow |
| `cw_flow_kg_s` | kg/s | Measured condenser-water mass flow |
| `compressor_power_kw` | kW | Measured compressor power |
| `suction_pressure_kpa` | kPa abs | Measured refrigerant suction pressure |
| `discharge_pressure_kpa` | kPa abs | Measured refrigerant discharge pressure |
| `cond_liquid_temp_c` | °C | Measured condenser liquid refrigerant temperature |
| `cooling_load_kw` | kW | Cooling load derived from measured chilled-water sensors |
| `cop` | dimensionless | Cooling load divided by measured compressor power |
| `fault_severity` | fraction | Ground-truth fouling loss or NCG progress |
| `condenser_ua_kw_per_k` | kW/K | Ground-truth condenser UA |
| `ncg_partial_pressure_kpa` | kPa | Ground-truth NCG partial pressure |
| `fault_active` | 0/1 | Ground-truth fault-start indicator |

## Expected fault signatures

As fouling reduces condenser UA, condenser saturation/discharge pressure,
compressor power, and condenser-water return temperature rise; COP falls.
At mid load, the expected final-sample change relative to healthy operation is
approximately `+3.8 K` condenser temperature, `+90 kPa` discharge pressure,
`-11%` COP, and `+13%` compressor power. Chilled-water-side behavior and
suction pressure should remain approximately unchanged. Exact sensor values
include seeded noise, and comparisons across periods should account for the
daily load and wet-bulb cycles.

## Fault separation

`derive_signals` adds apparent subcooling from discharge-pressure saturation
temperature minus measured condenser liquid temperature. Run
`python -m chiller_sim.separation` to compare 20 seeds of fouling, discharge-
pressure-matched Dalton and blanketed NCG, and UA-estimate-matched Dalton and
blanketed NCG. It writes per-seed metrics, interval-overlap summaries, and
plots under `reports/`. Separation is claimed only when a metric's observed
fouling range is disjoint from all four NCG ranges.

## Operating-point sweeps

`python -m chiller_sim.sweeps` compares noise-free steady-state load and
condenser-water-inlet sweeps with seeded sensor noise. It tests whether the
approach signatures remain separable as NCG partial pressure scales with
reference refrigerant pressure, and writes the sweep summary and figures under
`reports/`. The shared vectorized operating-point solver is exposed as
`chiller_sim.solve_operating_point`.
