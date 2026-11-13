# CPU oversubscription: final runs (2026-10-30)

All runs from branch `feature/cpu-quota-oversubscription`, clean tree
(`3c44e6e6`, timeout probe `7282d664`), started with `tools/cpu_oversubscription.sh`.
Console logs in `results/logs/`, figures and tables in `results/figures/`
(`scripts/cpu_oversubscription_plots.py --output results/figures results/cpu-oversubscription/*`).
Earlier pilot and smoke runs (dirty tree, superseded) are in `results/pilots/`.

| Run | Plan | Platform | Duration |
| --- | --- | --- | --- |
| `20261030-145728_aws` | `aws.json` (E1-E4) | Lambda eu-north-1, x86, Python 3.11 | 28 min |
| `20261030-152604_aws_timeout` | `aws-timeout.json` | Lambda eu-north-1 | 17 min |
| `20261030-145822_local` | `local.json` | Docker, 32-CPU workstation, cgroup v2 | 33 min |
| `20261030-153203_openwhisk_shares` | `openwhisk.json` | standalone OpenWhisk, cpu-shares only | 4 min |
| `20261030-154303_openwhisk_user-cpus` | `openwhisk.json` | standalone OpenWhisk, `user-cpus=32` (hard quota) | 8 min |

Failures: none, except the 8 intended timeouts of `e4-timeout-fine-oversubscribed`.

## Constraints of the AWS account

* Function memory is capped at 3008 MB (new account), so E1-E3 stop at 1.7 vCPUs.
* An organization service control policy denies Lambda in every region except eu-north-1,
  so the planned region comparison (`aws-regions.json`) could not run.
* Concurrency limit 10: batches of 8 concurrent invocations.

## Findings

**Lambda reports 2 CPUs at every memory size, but grants memory / 1769 MB vCPUs**
(`aws_effective_cpus`): 0.08 CPUs at 128 MB, 1.0 at 1769 MB, 1.7 at 3008 MB.
Two hardware populations: on hosts reporting `Intel Xeon Processor @ 2.50GHz` (58% of
invocations) the share matches memory / 1769 MB exactly; on hosts reporting
`Intel Xeon Processor` (40%) it is about 65% of that above 512 MB (1.1 CPUs at 3008 MB).
`cpu.stat` is not visible inside the Firecracker VM: the limit is enforced outside the guest.

**Worst thread count = reported CPUs.** libgomp spins at full strength only while its
threads do not exceed the CPUs in the affinity mask; the quota is invisible to it.
* Lambda, small grid: 2 threads take 2.0x (128 MB) and 2.2x (512 MB) the 1-thread time,
  3-8 threads only 1.1-1.2x (`penalty_*_aws_e2-thread-sweep-small`).
* OpenWhisk with quota, 8 co-located 1024 MB actions: 32 threads 6.2 s vs 0.023 s at 16 (271x),
  64 threads 0.15 s; shares only: 4.9 s vs 0.05 s.
* Local, quota of 2 CPUs on 32 visible: 32 threads 0.65 s vs 0.020 s at 8 (small grid),
  20.9 s vs 1.06 s (large grid); without limit 0.046 s and 0.35 s.

**The penalty is per synchronization.** At 128 MB, 2 threads cost 2.04x with a 1024 grid but
only 1.13x with a 2048 grid (4x work per barrier): `e4-timeout-*` vs `e2-thread-sweep-small`.

**Oversubscription alone causes timeouts** (`aws_timeout`, 128 MB, 1024 grid, 8000 iterations):
1 thread 125-186 s; 2 threads with default or active wait policy all 8 hit the 300 s timeout
(`Sandbox.Timedout`); 2 threads with `OMP_WAIT_POLICY=passive` 182-191 s.

**Wait policy** (`wait_policy_*`): at 128 MB, 2 threads: active 11.1 s, default 9.2 s,
passive 4.7 s. Local model of 128 MB: 3.2 / 2.9 / 1.3 s.

**Local model of Lambda** (`aws_against_local_model`): 2 cpuset CPUs with the measured quota
reproduces the 2-thread spike at 128 MB and 512 MB, but overestimates the penalty of many
threads (2.2-2.4x at 64 vs 1.5x on Lambda): CFS throttles the container's threads, whereas
Lambda throttles the 2 vCPUs of the VM, inside which the guest kernel schedules the threads.

**OpenWhisk without `user-cpus`** gives no CPU isolation: an idle invoker grants ~31.7 CPUs
to every action regardless of memory; under co-location each action gets its share
(4 CPUs for 8 x 1024 MB, 1 CPU for 32 x 256 MB) while still seeing 32 CPUs.

## Caveats

* The workstation was otherwise idle (load average in each `meta.json`), but not isolated.
* The local run's load average rose to 5 in the last configurations (its own competitor and
  container restarts), see `meta.json`.
* OpenWhisk is a single standalone node with the Docker container factory, not a cluster.

# ONNX Runtime inference (2026-11-13)

`20261113-103204_aws_onnx`, plan `aws-onnx.json`, commit `33578b85`, Lambda eu-north-1, 320 invocations, no failures.
`412.image-recognition-onnx`: ResNet-50, ONNX Runtime 1.16.3, 4 timed inferences per thread count.

* ONNX Runtime's default pool on Lambda is 2 threads at every memory size (it reads the VM's 2 vCPUs).
  Locally, pinned to 2 CPUs with cpuset, its default is still 32: it ignores the affinity mask.
* Spinning workers (ONNX Runtime default): time / best time grows with threads without the libgomp
  back-off: 1.2-1.3x at 2, 2.3-2.6x at 3, 2.8-3.2x at 4, 5.9-6.3x at 8 threads, at every memory size.
* With `session.intra_op.allow_spinning=0`, all thread counts stay within 1.25x of the best.
* At 640-1769 MB, 1 thread is best; the default 2 threads costs 16-34%. At 3008 MB (1.7 CPUs) 2 threads is best.
* The function needs about 530 MB: at 512 MB (pilot) it used 507 MB of 512 MB.
* Local ground truth `20261113-111205_local_onnx` (`local-onnx.json`): ONNX Runtime's default pool is 32 threads
  under every limit, including cpuset 0-1 (it ignores the affinity mask). Model of 640 MB Lambda
  (cpuset 0-1, 0.36 CPUs): 8 threads 4.15 s vs 1 thread 0.61 s (6.8x; Lambda 6.1x); passive 0.71 s.
* OpenWhisk `20261113-112805_openwhisk_onnx_user-cpus` (`openwhisk-onnx.json`, user-cpus=32): a 1024 MB action
  gets `cpu.max` 400000/100000 (4 CPUs) and sees 32; ONNX Runtime's default pool is 32.
  Alone: 32 threads 0.238 s vs 0.021 s at 8 (11x), passive 0.055 s. 8 co-located: 0.884 s vs 0.050 s (18x), passive 0.116 s.

# Barrier storm (2026-11-13)

Constant total work, the grid size sets the work between two barriers (one barrier per Jacobi sweep).
Figure `barrier_storm`.

* AWS `20261113-111143_aws_storm` (`aws-barrier-storm.json`, 1e8 point updates), 128 MB, 2 threads / 1 thread:
  1.1x (2048^2 points per barrier), 2.1x (1024^2), 7.5x (512^2), 28x (256^2), **58x (128^2: 57.9 s vs 1.0 s)**.
  4 threads stay at 1.1-2.5x (libgomp backs off beyond the reported CPUs).
  512 MB: at most 1.4x at 2 threads; 1769 MB: none.
* Local `20261113-113030_local_storm` (`local-barrier-storm.json`, 1e9 point updates, 10x AWS so a run spans
  many CFS periods): quota of 2 CPUs on 32 visible, 32 threads: 7.5x (2048^2) to 36x (128^2);
  model of 512 MB Lambda: 32 threads 1.5x to 33x, 2 threads 1.2-1.5x (matches Lambda at 512 MB).
  A local model of 128 MB (0.079 CPUs) was not run: runs would take hours under a 100 ms CFS period.
* An earlier local run with 1e8 updates finished within single CFS periods and is in `results/pilots/`.
