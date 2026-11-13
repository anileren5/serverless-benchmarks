# OpenWhisk standalone, first full run

Setup: `tools/openwhisk_standalone.sh` (Docker container factory, no `user-cpus`),
8704 MB user memory (8 GB for benchmark actions, 512 MB for two prewarmed Node.js containers).
Host: 32 hardware threads. Plan: `config/cpu-oversubscription/openwhisk.json`. No failures.

## Observations

- Mechanism: `cpu.max` = `max`, `cpu.weight` = 7 / 21 / 59 for 256 / 1024 / 4096 MB, i.e.
  `--cpu-shares` = 1024 * memory / user memory. Every action reports 32 CPUs.
  Matches the `shares` fingerprint of the local ground truth.
- Idle host (`ow-idle`): about 31 effective CPUs at every memory size and identical speedup curves.
  Unlike Lambda, memory buys no CPU on an idle OpenWhisk node.
- 8 co-located 1024 MB actions: each gets about 3.9 effective CPUs (32 / 8) while reporting 32.
  Default wait policy: 4868 ms at 32 threads versus 36.6 ms at 8 threads (133x).
  Passive: 222 ms at 32 threads.
- 32 co-located 256 MB actions (`test` input, barrier-dominated): about 1 effective CPU each;
  12.1 s at 32 threads versus 0.4 ms at 4 threads.
- Unexplained: with 8 co-located actions, 1 thread takes 857 ms versus 84 ms when idle, slower
  than 8 threads (37 ms). The 1-thread run directly follows the 32-thread probe of every action,
  so it overlaps with the other actions' probes. Needs a run with a different kernel order.

## Caveats

- Invocations in a batch start together but drift apart, so the level of contention varies over
  an invocation.
- `wsk action invoke --result` blocks for at most 60 s; longer invocations would be recorded as
  failures.
