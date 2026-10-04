# Detector Characteristics

This document reports the false-positive rate (FPR) and true-positive rate
(TPR) of each of the seven anomaly detectors that ship with BlackBoxRS
v0.4.1, measured on a deterministic synthetic event stream. The numbers
below are a **floor** on FPR and a **ceiling** on TPR: real telemetry has
NTP step events, container PID-namespace quirks, DDS jitter, and other
sources of structured noise that the synthetic stream does not reproduce.
Calibration against a real-robot capture is owed and tracked in the
project README's "What is planned" section.

## How to reproduce

```bash
python scripts/measure_detector_fpr.py --hours 24
python scripts/measure_detector_fpr.py --hours 1 --json-output artifacts/detector_characteristics.json
```

The harness runs each detector against a 24-hour stream of 1Hz samples
drawn from a calibrated noise distribution, then runs the same detector
against a stream that injects one violation per hour. Deterministic seed
(`0xB1ACB07`); reruns produce identical numbers.

The measurement uses `min_consecutive_samples = 2` (the v0.4.1 hysteresis
default) for the four threshold-based detectors (`threshold`, `frequency`,
`clock_skew`, `process_signals`).

## v0.4.1 reference numbers (24-hour synthetic stream)

| Detector | FPR (fires/hr) | TPR | Median time-to-fire (s) |
|---|---|---|---|
| `threshold` | 0.000 | 1.00 | 1.0 |
| `frequency` | 0.000 | 1.00 | 1.0 |
| `dead_topic` | n/a | n/a | n/a |
| `qos_mismatch` | 0.000 | 1.00 | 0.5 |
| `tf_topology` | 0.000 | 1.00 | 0.5 |
| `clock_skew` | 0.000 | 1.00 | 1.0 |
| `process_signals` | 0.000 | 1.00 | 1.0 |

**Interpretation.** Six detectors show zero false positives across a
simulated 24-hour stream of 1Hz samples and fire on every injected
violation within 1.0 simulated second. That is what the synthetic-noise
budget predicts (per-sample distributions are calibrated to stay well
below detector thresholds), and the hysteresis introduced in `5d3ab4f`
collapses single-sample jitter below the fire condition.

`dead_topic` reports `n/a`: its trigger condition is wall-clock-based
(`Clock.now()` inside `dead_topic.py:77`), and the accelerated synthetic
stream runs all 86,400 samples in roughly 3 seconds of real wall time, so
no event ever exceeds the 5s silence threshold. This detector requires a
real-time measurement and is exercised live in
`tests/integration/test_ros_live.py` (Docker Humble CI job).

## What this measurement deliberately does NOT claim

The synthetic stream does not reproduce, and these numbers are not a
substitute for measuring against:

- **Real DDS jitter.** Burst-arrival of late frames, lossy multicast, and
  RMW retransmits all change the per-sample frequency distribution.
- **Real NTP step events.** A live NTP daemon will occasionally slam the
  system clock by 50 ms or more, which the synthetic stream does not
  emit. This is the single most important gap for `clock_skew`.
- **Real process scheduling.** psutil CPU% on a real Linux host is delta-
  based with kernel-jiffy quantization; the synthetic stream draws from
  a clean Gaussian.
- **Container PID-namespace effects.** If the daemon runs in a different
  PID namespace from ROS nodes, the producer sees nothing. Synthetic
  measurement cannot expose this.
- **Multi-node TF chains.** The TF test exercises a single edge; real
  robots have 20+ edges with multiple publishers, and the multi-parent
  detection path is not stressed by this stream.
- **Wall-clock dependencies.** `dead_topic` requires real-time elapsed
  windows and is unmeasured here; see note above.

## Future work

1. **Real-robot bundle.** A captured live onboard GO2 session is owed (see
   README "What is planned"). Once it exists, this harness should be
   re-pointed at the captured event stream and the numbers re-computed.
2. **Real-time `dead_topic` measurement.** Either run the harness with a
   monkey-patched clock that advances per sample, or split out a
   dedicated wall-clock harness that uses `time.sleep` between samples
   for a small N.
3. **NTP step injection.** Add an explicit NTP-step scenario to the
   `clock_skew` violation stream: shift the system source by 80ms once
   per simulated hour and re-measure FPR with hysteresis enabled.
4. **Burstiness scenarios for `frequency`.** Real publishers send in
   bursts under load; replace the Gaussian noise with a bursty arrival
   process and re-measure FPR.
