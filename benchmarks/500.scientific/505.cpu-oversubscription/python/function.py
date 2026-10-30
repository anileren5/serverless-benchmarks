import ctypes
import os
import random
import time

_lib = None

# cgroup v2 files first, then their cgroup v1 counterparts.
CGROUP_FILES = [
    "/sys/fs/cgroup/cpu.max",
    "/sys/fs/cgroup/cpu.weight",
    "/sys/fs/cgroup/cpu.stat",
    "/sys/fs/cgroup/cpuset.cpus.effective",
    "/sys/fs/cgroup/memory.max",
    "/sys/fs/cgroup/cpu/cpu.cfs_quota_us",
    "/sys/fs/cgroup/cpu/cpu.cfs_period_us",
    "/sys/fs/cgroup/cpu/cpu.shares",
    "/sys/fs/cgroup/cpu/cpu.stat",
    "/sys/fs/cgroup/memory/memory.limit_in_bytes",
]


def load_library(omp_wait_policy):
    """libgomp reads OMP_* variables once, when the library is loaded.
    In a warm container, the policy of the first invocation stays in effect."""
    global _lib
    if _lib is None:
        if omp_wait_policy is not None:
            os.environ["OMP_WAIT_POLICY"] = omp_wait_policy
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kernels.so")
        lib = ctypes.CDLL(path)
        lib.jacobi.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_double),
            ctypes.POINTER(ctypes.c_double),
            ctypes.POINTER(ctypes.c_double),
        ]
        lib.jacobi.restype = ctypes.c_double
        lib.spin.argtypes = [
            ctypes.c_int,
            ctypes.c_double,
            ctypes.POINTER(ctypes.c_double),
            ctypes.POINTER(ctypes.c_double),
        ]
        _lib = lib
    return _lib


def read_file(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def parse_stat(content):
    if content is None:
        return None
    return {key: int(value) for key, value in (line.split() for line in content.splitlines())}


def cpu_stat():
    for path in ("/sys/fs/cgroup/cpu.stat", "/sys/fs/cgroup/cpu/cpu.stat"):
        stat = parse_stat(read_file(path))
        if stat is not None:
            return stat
    return None


def stat_delta(before, after):
    if before is None or after is None:
        return None
    return {key: after[key] - before[key] for key in after if key in before}


def cpu_model():
    content = read_file("/proc/cpuinfo")
    if content is None:
        return None
    for line in content.splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip()
    return None


def cpus_allowed_list():
    content = read_file("/proc/self/status")
    if content is None:
        return None
    for line in content.splitlines():
        if line.startswith("Cpus_allowed_list"):
            return line.split(":", 1)[1].strip()
    return None


def environment(lib):
    return {
        "os_cpu_count": os.cpu_count(),
        "sched_affinity": len(os.sched_getaffinity(0)),
        "cpus_allowed_list": cpus_allowed_list(),
        "omp_num_procs": lib.omp_reported_procs(),
        "omp_default_threads": lib.omp_default_threads(),
        "omp_wait_policy": os.environ.get("OMP_WAIT_POLICY"),
        "cpu_model": cpu_model(),
        "cgroup": {path: read_file(path) for path in CGROUP_FILES if os.path.exists(path)},
    }


def default_thread_counts(reported_cpus):
    counts = []
    threads = 1
    while threads <= 4 * reported_cpus and threads <= 128:
        counts.append(threads)
        threads *= 2
    return counts


def measure_effective_cpus(lib, threads, seconds):
    wall = ctypes.c_double()
    cpu = ctypes.c_double()
    lib.spin(threads, seconds, ctypes.byref(wall), ctypes.byref(cpu))
    return {
        "threads": threads,
        "wall_seconds": wall.value,
        "cpu_seconds": cpu.value,
        "effective_cpus": cpu.value / wall.value,
    }


def run_jacobi(lib, grid_size, iterations, threads):
    wall = ctypes.c_double()
    cpu = ctypes.c_double()
    checksum = ctypes.c_double()
    before = cpu_stat()
    status = lib.jacobi(
        grid_size,
        iterations,
        threads,
        ctypes.byref(wall),
        ctypes.byref(cpu),
        ctypes.byref(checksum),
    )
    after = cpu_stat()
    if status != 0.0:
        raise MemoryError(f"Jacobi grid of size {grid_size}x{grid_size} could not be allocated")
    return {
        "threads": threads,
        "wall_seconds": wall.value,
        "cpu_seconds": cpu.value,
        "checksum": checksum.value,
        "cgroup_cpu_stat_delta": stat_delta(before, after),
    }


def idle(seconds):
    """Pause between kernels so OpenMP workers stop spinning before the next measurement.
    CPU time consumed during the pause is spent by workers that are still spinning."""
    cpu_begin = time.process_time()
    time.sleep(seconds)
    return time.process_time() - cpu_begin


def handler(event):
    grid_size = event["grid_size"]
    iterations = event["iterations"]
    spin_seconds = event["spin_seconds"]
    idle_seconds = event.get("idle_seconds", 0.0)

    order = event.get("order", "ascending")
    seed = event.get("seed", random.randrange(2**32))

    lib = load_library(event.get("omp_wait_policy"))
    env = environment(lib)
    thread_counts = list(event.get("threads") or default_thread_counts(env["os_cpu_count"]))
    if order == "shuffled":
        random.Random(seed).shuffle(thread_counts)
    elif order != "ascending":
        raise ValueError(f"Unknown order {order}, expected ascending or shuffled")

    effective = measure_effective_cpus(lib, env["os_cpu_count"], spin_seconds)
    effective["idle_cpu_seconds_after"] = idle(idle_seconds)

    jacobi_runs = []
    for position, threads in enumerate(thread_counts):
        run = run_jacobi(lib, grid_size, iterations, threads)
        run["position"] = position
        run["idle_cpu_seconds_after"] = idle(idle_seconds)
        jacobi_runs.append(run)

    return {
        "result": {
            "environment": env,
            "effective_cpus": effective,
            "jacobi": jacobi_runs,
            "order": order,
            "seed": seed,
        },
        "measurement": {
            "grid_size": grid_size,
            "iterations": iterations,
            "idle_seconds": idle_seconds,
        },
    }
