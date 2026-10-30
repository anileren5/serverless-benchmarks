# CPU Oversubscription

OpenMP, BLAS, and similar runtimes size their thread pools from the number of CPUs the OS reports
(`os.cpu_count()`, `omp_get_num_procs()`), which ignores CPU quotas imposed by cgroups or by the
hypervisor. A function that sees 32 CPUs but is granted 2 starts 32 threads, which are then
time-sliced and throttled. Kernels that synchronize often suffer most, because every barrier waits
for the thread that was descheduled last.

## Kernel

`python/kernels.c` contains:

* `jacobi`: a 2D Jacobi stencil for the Laplace equation on an `n x n` grid. Every sweep replaces
  each interior point by the average of its four neighbours and ends with the implicit barrier of
  `omp for`, so one call performs `iterations` barriers.
* `spin`: busy-loops a number of threads for a fixed wall time and reports the consumed process CPU
  time. CPU time divided by wall time is the number of CPUs the sandbox actually grants.

The library is compiled with `-O3 -fopenmp` in `python/package.sh`, inside the build image.
`libgomp.so.1` is copied next to `kernels.so` and resolved through `rpath=$ORIGIN`, because function
runtimes do not ship it. `python/init.sh` copies the C sources into the package.

## Input

| Field | Meaning |
| :--- | :--- |
| `grid_size` | Grid edge length `n`. Memory use is `2 * n^2 * 8` bytes. |
| `iterations` | Number of Jacobi sweeps, i.e., barriers. |
| `spin_seconds` | Duration of the effective-CPU probe. |
| `idle_seconds` | Pause after every kernel, so spinning OpenMP workers settle before the next measurement. |
| `threads` | Optional list of thread counts. Default: powers of two up to `4 * os.cpu_count()`, at most 128. |
| `omp_wait_policy` | Optional `active` or `passive`. Applied only when the library is first loaded in a container. |
| `order` | `ascending` (default) or `shuffled` order of thread counts; shuffling removes position effects, e.g., from co-located functions. |
| `seed` | Optional seed of the shuffle; a random seed is drawn and reported otherwise. |

Sizes `test`, `small`, and `large` use grids of 128, 1024, and 2048 points with 100, 200, and 500 sweeps.

## Output

* `environment`: CPUs reported by Python, the scheduler affinity, OpenMP, `/proc/self/status`,
  the CPU model, and all available cgroup v1/v2 CPU and memory files.
* `effective_cpus`: result of the `spin` probe with `os.cpu_count()` threads.
* `jacobi`: per thread count, its position in the execution order, wall time, process CPU time,
  grid checksum, and the change of the cgroup `cpu.stat` counters (`nr_throttled`,
  `throttled_usec`) during the run.
* `order` and `seed`: the execution order of thread counts.

Checksums must be identical across thread counts; `validate_output` checks this.

The [`cpu-oversubscription` experiment](../../../docs/experiments.md#cpu-oversubscription) sweeps
this benchmark over memory sizes in the cloud and over Docker CPU limits locally.

With the default `OMP_WAIT_POLICY`, OpenMP workers keep spinning for a while after a parallel
region ends. `idle_cpu_seconds_after` is the process CPU time consumed during the pause that
follows each kernel, i.e., how much CPU the spinning workers burned. With `active`, workers spin
far longer than any reasonable pause and also consume CPU during the next measurement.
