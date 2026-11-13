# 412.image-recognition-onnx

ResNet-50 v1 (ONNX model zoo, 98 MB) classification with ONNX Runtime 1.16 on the CPU,
run at several intra-op thread counts within one invocation.
ONNX Runtime and the model fit into a Lambda zip package (about 180 MB unpacked),
unlike the PyTorch build of `411.image-recognition` for recent Python versions.

The benchmark measures how ONNX Runtime's thread pool behaves under CPU quotas:
the default pool size is derived from the CPUs of the machine, and idle workers spin
unless `session.intra_op.allow_spinning` is disabled. It is used by the
`cpu-oversubscription` experiment (`config/cpu-oversubscription/aws-onnx.json`).

## Input

| Field | Meaning |
| --- | --- |
| `inferences` | Timed inferences per thread count, after one warm-up inference. |
| `threads` | Intra-op thread counts; default `[1, 2, 3, 4, 6, 8]`. |
| `omp_wait_policy` | `active` or `passive` switches spinning on or off; ONNX Runtime's default (spinning) otherwise. |
| `idle_seconds` | Pause after each run, so spinning workers do not run into the next measurement. |
| `order`, `seed` | `ascending` or `shuffled` order of thread counts. |

The input tensor is fixed and random: the image decoding of `411.image-recognition` does not use the thread pool.

## Output

* `environment`: CPUs reported by the OS and the affinity mask, ONNX Runtime version,
  the pool size ONNX Runtime chooses by default, CPU model, and cgroup files.
* `default_run`: the run with ONNX Runtime's default thread count.
* `runs`: per thread count, the pool size (worker threads created plus the calling thread),
  wall and process CPU time, time per inference, top-1 class, and a checksum of the logits.

`validate_output` checks that all thread counts predict the same class.
