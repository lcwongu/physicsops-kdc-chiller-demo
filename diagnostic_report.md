# Chiller Diagnostic Report

## Assumptions

This analysis uses the steady-state synthetic chiller model and the sensor
noise specified in the project documentation. The comparison windows are
selected from the documented fault schedule: the healthy period ends at the
scheduled fault start and the degraded period is the final 24 hours. Diagnosis
and comparisons use measured sensor columns only; ground-truth labels are
reserved for validation.

## Fault schedule

The simulated condenser fouling ramp starts at `2025-07-04 00:00` and reaches
50% UA loss at the final sample, compressing a fault that would typically
develop over weeks to months into four days.

## Measured changes

The table compares hour-of-day-matched means for healthy operation and the
final 24 hours. Slopes are load- and weather-adjusted hourly trend estimates.

<!-- BEGIN GENERATED: measured-changes -->

| signal | healthy | degraded | Δ | Δ% | healthy slope/day | fault slope/day |
| --- | --- | --- | --- | --- | --- | --- |
| ua_cond_est_kw_per_k | 199.992 | 112.716 | -87.276 | -43.640% | -0.080 | -24.755 |
| condenser_approach_c | 2.315 | 5.352 | 3.037 | 131.194% | 0.000 | 0.884 |
| cond_sat_temp_c | 33.914 | 37.269 | 3.355 | 9.892% | 0.002 | 0.897 |
| discharge_pressure_kpa | 862.507 | 948.674 | 86.167 | 9.990% | -0.149 | 22.603 |
| cw_supply_temp_c | 27.900 | 28.153 | 0.253 | 0.908% | 0.005 | -0.002 |
| cw_return_temp_c | 31.599 | 31.917 | 0.318 | 1.005% | 0.001 | 0.013 |
| cw_range_c | 3.700 | 3.764 | 0.064 | 1.740% | -0.004 | 0.015 |
| compressor_power_kw | 130.191 | 144.500 | 14.309 | 10.990% | -0.193 | 3.286 |
| cop | 5.193 | 4.661 | -0.532 | -10.248% | -0.005 | -0.146 |
| suction_pressure_kpa | 341.070 | 341.167 | 0.098 | 0.029% | — | — |
| chw_supply_temp_c | 7.001 | 6.999 | -0.002 | -0.026% | — | — |
| cooling_load_kw | 644.488 | 642.778 | -1.711 | -0.265% | — | — |
| cw_flow_kg_s | 50.009 | 50.020 | 0.011 | 0.023% | — | — |
| outdoor_wet_bulb_c | 23.899 | 24.158 | 0.259 | 1.083% | — | — |

<!-- END GENERATED: measured-changes -->

Across the full four-day fault window, the measured discharge-pressure, power,
and COP effects are about half the final-24-hour changes: approximately
`+43 kPa`, `+6.3%`, and `−5.3%`. The final-24-hour CW-return increase of
`+0.318 °C` is mostly weather-related: CW supply rose `+0.253 °C` (wet bulb
`+0.259 °C`). The fault's own condenser-water range rise is about `+0.07 °C`,
consistent with `ΔW/(m_cw·cp) = 14.3/209.3`.

## Diagnostic reasoning

The sensor-derived causal chain is decreasing condenser UA → increasing
condenser approach → increasing condensing saturation temperature and discharge
pressure → increasing lift → decreasing COP → increasing compressor power,
with a slight increase in CW range. Suction pressure, chilled-water supply,
cooling load, and flow remain approximately unchanged in the final-24-hour
matched comparison.

| Hypothesis | Expected signature | Consistent | Notes |
|---|---|---:|---|
| Condenser heat-transfer degradation | UA estimate falls while approach and discharge pressure rise | Yes | Consistent with waterside fouling or scaling |
| Elevated CW supply / tower-weather | CW supply rise accounts for condensing-temperature rise; approach stays small | No | Does not explain the measured approach increase |
| Reduced CW flow | CW flow decreases by at least 5% | No | Flow is approximately unchanged |
| Evaporator-side fault or refrigerant undercharge | Suction pressure falls by at least 10 kPa | No | Suction pressure is approximately unchanged |
| Increased cooling load | Cooling load increases by at least 6% | No | Cooling load is approximately unchanged |
| Non-condensables in the condenser | Condenser pressure and approach rise with apparent UA loss | Yes | These sensors can't tell it apart from fouling; it would need a refrigerant liquid temperature vs. saturation check, or purge history |

Conclusion: the data support condenser heat-transfer degradation. Non-condensables
cannot be excluded by these sensors alone.

## Validation against ground truth

The sensor-only UA estimate agrees with the ground-truth daily mean UA to
within 3% on each day. The estimates are approximately `199.95`, `200.11`,
`199.92`, `186.95`, `162.54`, `137.64`, and `112.62 kW/K`, compared with
ground truth `200`, `200`, `200`, `187.5`, `162.5`, `137.5`, and `112.5 kW/K`.
The condenser approach grows from approximately `2.3 °C` to `5.4 °C` by day 7.
Ground truth is shown only in the validation trace below.

![Hourly diagnostic time series](reports/diagnostic_timeseries.png)

![Healthy and degraded hour-of-day profiles](reports/diagnostic_hourly_profiles.png)

![Hour-matched signal changes](reports/diagnostic_changes.png)

## Limitations

The fault ramp is accelerated to four days rather than its weeks-to-months
physical timescale. The simulation is synthetic and steady-state, with no
transient equipment dynamics, fixed water flows, and a constant tower
approach. This report uses one random seed; the regression tests separately
exercise multiple seeds.

## Test weakness and recovery

The original single-seed direction test used CW return temperature and a tight
cooling-load tolerance. CW return was confounded by wet-bulb AR(1) drift
between the comparison windows (about ±0.19 °C), larger than the fault's own
approximately `+0.07 °C` CW-range effect. The exogenous cooling-load AR(1)
variation also moved the window mean independently of the fault. The 50-seed
regression produced this failure summary:

```text
FAILED tests/test_telemetry.py::test_degradation_direction_across_seeds[3] - ...
FAILED tests/test_telemetry.py::test_degradation_direction_across_seeds[4] - ...
FAILED tests/test_telemetry.py::test_degradation_direction_across_seeds[12]
FAILED tests/test_telemetry.py::test_degradation_direction_across_seeds[14]
FAILED tests/test_telemetry.py::test_degradation_direction_across_seeds[15]
FAILED tests/test_telemetry.py::test_degradation_direction_across_seeds[21]
FAILED tests/test_telemetry.py::test_degradation_direction_across_seeds[24]
FAILED tests/test_telemetry.py::test_degradation_direction_across_seeds[29]
FAILED tests/test_telemetry.py::test_degradation_direction_across_seeds[30]
FAILED tests/test_telemetry.py::test_degradation_direction_across_seeds[34]
FAILED tests/test_telemetry.py::test_degradation_direction_across_seeds[35]
FAILED tests/test_telemetry.py::test_degradation_direction_across_seeds[36]
FAILED tests/test_telemetry.py::test_degradation_direction_across_seeds[38]
FAILED tests/test_telemetry.py::test_degradation_direction_across_seeds[39]
FAILED tests/test_telemetry.py::test_degradation_direction_across_seeds[41]
FAILED tests/test_telemetry.py::test_degradation_direction_across_seeds[43]
FAILED tests/test_telemetry.py::test_degradation_direction_across_seeds[44]
>       assert deltas["cw_return_temp_c"] > 0.0
E       assert -0.22050713544261713 > 0.0

17 failed, 39 passed in 1.70s
```

The cooling-load difference is exogenous AR(1) variation, with a 1.6% standard
deviation and a 4.7% maximum over the verified 200-seed set.

Recovery: retain the pressure, power, COP, suction, and chilled-water checks;
use condenser approach and sensor-only UA loss instead of CW return as the
fault-specific signals, and allow a 6% cooling-load difference to accommodate
the documented exogenous noise. The telemetry suite passed after this change:
`56 passed in 1.79s`.

## Reproduce

```bash
pip install -r requirements.txt
python -m chiller_sim --seed 42 --output data/chiller_telemetry.csv
python -m chiller_sim.report
python -m pytest -q
```
