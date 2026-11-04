# Results (reference run)

These numbers come from the **bundled demo network** (stylised 5×5 grid, 9 signalised junctions, left-hand traffic) and the **built-in engine**. They show the evaluation method works end to end. For your report, re-run on the imported Nerul–Seawoods OSM network (and ideally SUMO) and report those numbers instead.

Reproduce:

```bash
python simulation/run_simulation.py experiment --seeds 1 2 3 4 5 6 7 8 9 10
python -m computer_vision.evaluation.evaluate --seconds 300 --out results/cv_evaluation.json
```

Scenario: ambulance from base A1 (west) → pickup at junction n23 → hospital H2 (south-east); 300 s warm-up; demand low/medium/high = 900/1800/2700 vehicles per hour inserted across the network. Each (demand, seed) pair is run twice with identical background traffic: fixed-time signals only (baseline) vs route-aware priority + adaptive recovery. 10 seeds per demand level, 60 simulations, ~1 minute of compute.

## Ambulance (mean ± 95% CI, n = 10 per cell)

| Demand | Driving time baseline (s) | Driving time priority (s) | Paired difference (s) | Signal delay baseline → priority (s) | Stops baseline → priority |
|---|---|---|---|---|---|
| low | 306.0 ± 18.5 | 277.0 ± 11.7 | **−29.0 ± 17.3** | 36.2 → 5.6 | 1.3 → 0.1 |
| medium | 314.5 ± 20.4 | 284.6 ± 8.1 | **−29.9 ± 18.1** | 36.9 → 8.0 | 1.4 → 0.0 |
| high | 355.0 ± 21.2 | 318.0 ± 20.8 | **−37.0 ± 22.7** | 59.7 → 23.2 | 1.5 → 0.0 |

Driving time = to pickup + to hospital (excludes the 30 s pickup dwell). Reduction ≈ 9.5–10.4%. Priority also made the trip more predictable (smaller spread at low/medium demand). At high demand the ambulance is still delayed by queues that form upstream of the detection range and by spill-back between junctions.

## Other traffic and safety

| Demand | Other vehicles' mean time loss, paired difference (s) | Mean green hold per junction (s) | Max cross-street queue during priority (veh) | Recovery time after passage (s) | Recovery timeouts | Conflicting greens |
|---|---|---|---|---|---|---|
| low | +0.3 ± 1.1 | 6.4 | 0.5 | 2.8 ± 2.9 | 0 | 0 |
| medium | +0.3 ± 1.0 | 9.0 | 2.2 | 6.5 ± 3.2 | 0 | 0 |
| high | −2.1 ± 1.0 | 15.1 | 3.1 | 5.1 ± 4.3 | 0 | 0 |

Other traffic is measured over a fixed 600 s departure window after dispatch in both arms, so the comparison is not biased by the ambulance arriving sooner. On this network, a single ambulance's priority had no measurable network-wide cost at low/medium demand; at high demand the adaptive recovery timings slightly reduced average delay. Recoveries are short because holds are short and the grid is not oversaturated – expect longer recoveries (and possibly timeouts) on a congested real network.

The SUMO engine gives the same picture with more variance (3 seeds: −8 to −39 s driving time, 0 conflicting greens); run `--engine sumo` with more seeds for the report.

## Computer vision (simulated cameras, medium demand, 300 s, 24 approaches, 1,440 scored frames)

| Metric | Value |
|---|---|
| Detection precision (IoU ≥ 0.5) | 0.999 |
| Detection recall | 0.902 |
| F1 | 0.948 |
| Vehicle count MAE | 0.06 vehicles/frame |
| Queue MAE (vehicles in camera view) | 0.04 |
| Queue bias | −0.01 |
| Queue MAE vs whole approach | 0.04 |

Recall misses are mostly vehicles partly outside the frame (IoU < 0.5) and grey cars close to the asphalt colour. These figures are on clean rendered frames and **do not** indicate real-CCTV performance; use `computer_vision/detect_video.py` (YOLO) on real footage for that.

## How to present these honestly

* Say "in simulation, on network X, with N seeds" and show CIs, not a single run.
* Report both the ambulance benefit and the cost to other traffic.
* State that thresholds (lead-time margin, recovery tolerance, max hold) are design parameters you chose, and show one sensitivity run if time allows (e.g. `PriorityConfig.safety_margin_s` 3 vs 6 vs 10 s).
