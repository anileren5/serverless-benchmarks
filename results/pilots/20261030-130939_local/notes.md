# Local ground truth, first full run

Plan: `config/cpu-oversubscription/local.json`. Host: 32 hardware threads, cgroup v2, Docker.
The host was not otherwise reserved for the run. 10 repetitions per cell, no failures.

## Observations

- CFS quota limits CPU time per 100 ms period, not parallelism. The `small` kernel needs about
  85 ms of CPU time, so with `quota-2` up to 8 threads finish inside one period without being
  throttled (19.9 ms at 8 threads). The same holds for `model-1769MB` (1 CPU quota, 2 threads
  faster than 1). Steady-state behaviour needs kernels much longer than the period: use `large`.
- With a quota, the penalty appears once the kernel overruns the period: 123 ms at 16 threads and
  848 ms at 32 threads (`quota-2`), versus 20 ms at best.
- Penalties peak at threads == reported CPUs (32), not at 64. libgomp shortens spin-waiting when
  threads exceed the CPUs it detects (`gomp_throttled_spin_count_var`); at 32 threads it still
  spins at full length. The detected count comes from the affinity mask, so a quota leaves it at 32.
- `cpuset-2`: `omp_get_num_procs()` = 2, `os.cpu_count()` = 32. Speedup saturates at 2 and the
  oversubscription penalty is mild (94 ms at 64 threads versus 46 ms at 2).
- Shares are soft: `shares-idle` is indistinguishable from `unlimited`. With a competitor,
  32 threads take 7.2 s versus 94 ms at 2 threads (77x); the probe measures about 4 CPUs.
- Wait policy at 32 threads, `quota-2`: default 1005 ms, active 764 ms, passive 149 ms.
  Without a limit: default 82 ms, passive 22 ms.
- AWS model (cpuset of 2 CPUs + quota of memory / 1769 MB): probe gives 0.083, 0.30, 1.15 CPUs
  for 128, 512, 1769 MB; every period is throttled at 128 and 512 MB.

## Follow-up

- Repeat `gt-mechanisms` and `gt-aws-model` with the `large` input to get past the period-scale
  burst regime.
