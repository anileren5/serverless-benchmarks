# 506.data-parallel-sgd

One rank of bulk-synchronous, data-parallel training: logistic regression on a synthetic shard per
rank, with the gradients all-reduced through object storage at every iteration
(each rank publishes its gradient under `<run_id>/<iteration>/<rank>` and polls for all others).
This is distributed SGD with gradient averaging; the same pattern appears in iterative solvers
with halo exchanges or global reductions.

A run only progresses if all `world_size` ranks execute concurrently. It is launched by the
`gang-scheduling` experiment (see `docs/experiments.md`), which invokes all ranks at once.
A rank that waits longer than `barrier_timeout` at a barrier stops and reports `barrier_timeout`.

The SeBS storage wrappers randomise uploaded key names, so the function uses the underlying
S3 or MinIO client for exact keys.

## Input

`rank`, `world_size`, `run_id`, `iterations`, `samples_per_rank`, `features`, `learning_rate`,
`barrier_timeout`, `poll_interval`, and the output bucket.

## Output

`status` (`completed` or `barrier_timeout`), completed iterations, timestamps (begin, data ready, end),
the loss per iteration, a checksum of the final weights (equal on all ranks of a completed run),
and per iteration the compute, publish, and barrier wait times.
