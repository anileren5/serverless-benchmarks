# Gang scheduling: final runs (2026-11-13)

Branch `feature/cpu-quota-oversubscription`, commit `f62e77ad`, clean tree.
`EXPERIMENT=gang-scheduling tools/cpu_oversubscription.sh <platform> config/gang-scheduling/<plan>.json`.
Figure and table: `results/figures/gang_scheduling.{png,pdf,csv}` (`scripts/gang_scheduling_plots.py`).

Workload: `506.data-parallel-sgd`, data-parallel logistic regression (20,000 samples x 256 features per rank,
20 iterations); every iteration all-reduces the gradients through object storage (S3 on AWS, MinIO on
OpenWhisk). A rank gives up after `barrier_timeout` (AWS 30 s, OpenWhisk 20 s) at a barrier.
3 launches per N.

| Run | Platform | Predicted cap | Completed | Deadlocked |
| --- | --- | --- | --- | --- |
| `20261113-120703_aws` | Lambda eu-north-1, 1024 MB | 10 (account concurrent executions) | N = 2-10 (all) | N = 11, 12, 16 (all) |
| `20261113-120711_openwhisk_1024MB` | standalone OpenWhisk | 8192 MB / 1024 MB = 8 | N = 2-8 (all) | N = 9, 10, 12 (all) |
| `20261113-121519_openwhisk_2048MB` | standalone OpenWhisk | 8192 MB / 2048 MB = 4 | N = 2-4 (all) | N = 5, 6 (all) |

The cap predicts every outcome (60 of 60 launches). The failure differs per platform:

* **AWS rejects** the ranks above the cap: `TooManyRequestsException ... Rate Exceeded` after boto's
  4 retries (N - 10 ranks). The 10 running ranks wait at the first barrier until the timeout (30.3 s makespan),
  billed for the whole wait.
* **OpenWhisk queues** them: the invoker has no memory for more containers, so the excess activations wait
  until the running ranks time out (start skew 20.1 s = barrier_timeout), then start and time out themselves
  (40.2 s makespan). No error is raised; without a timeout at the barrier the run would hang until the
  action timeout.

Below the cap the all-reduce through storage dominates: ranks spend 40 % (N=2) to 83 % (N=10) of their time
at barriers on AWS and about 72-77 % on OpenWhisk. Start skew below the cap is at most 20 ms (warm and cold).
Completed runs end with identical weights on all ranks.
