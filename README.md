# Results: pushing the envelope (DPHPC 2026)

Data branch for the `cpu-oversubscription` and `gang-scheduling` experiments.
The code is on `feature/cpu-quota-oversubscription`; every run's `meta.json` names the commit it ran from.

* `results/cpu-oversubscription/` and `results/gang-scheduling/`: one directory per run
  (`config.json`, `meta.json`, `raw.jsonl`, processed CSVs) and `NOTES.md` with the findings.
* `results/figures/`: figures and tables, regenerated with `scripts/cpu_oversubscription_plots.py`
  and `scripts/gang_scheduling_plots.py`.
* `results/logs/`: console logs of the runs. `results/pilots/`: superseded pilot runs.
