"""ResNet-50 inference with ONNX Runtime at several intra-op thread counts.

ONNX Runtime sizes its intra-op thread pool from the CPUs the process can see and,
by default, lets idle workers spin. Under a CPU quota both choices can backfire,
which is what this benchmark measures in a common serverless inference setup.
"""

import gc
import os
import random
import resource
import time

import numpy as np
import onnxruntime as ort

MODEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resnet50-v1-7.onnx")

# cgroup v2 files first, then their cgroup v1 counterparts.
CGROUP_FILES = [
    "/sys/fs/cgroup/cpu.max",
    "/sys/fs/cgroup/cpu.weight",
    "/sys/fs/cgroup/cpuset.cpus.effective",
    "/sys/fs/cgroup/memory.max",
    "/sys/fs/cgroup/cpu/cpu.cfs_quota_us",
    "/sys/fs/cgroup/cpu/cpu.cfs_period_us",
    "/sys/fs/cgroup/cpu/cpu.shares",
]

# The experiment's OpenMP wait policies map onto ONNX Runtime's spinning switch.
SPINNING = {None: None, "active": "1", "passive": "0"}


def read_file(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def status_field(name):
    for line in (read_file("/proc/self/status") or "").splitlines():
        if line.startswith(name + ":"):
            return line.split(":", 1)[1].strip()
    return None


def cpu_model():
    for line in (read_file("/proc/cpuinfo") or "").splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip()
    return None


def session(threads, spinning):
    """threads=0 is ONNX Runtime's default; the returned pool size counts the calling thread,
    which runs work too, so it is the number of worker threads the session created plus one."""
    options = ort.SessionOptions()
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    if spinning is not None:
        options.add_session_config_entry("session.intra_op.allow_spinning", spinning)
    before = int(status_field("Threads"))
    sess = ort.InferenceSession(MODEL, options, providers=["CPUExecutionProvider"])
    return sess, int(status_field("Threads")) - before + 1


def cpu_seconds():
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


def run_inference(threads, spinning, batch, inferences):
    sess, pool = session(threads, spinning)
    name = sess.get_inputs()[0].name
    sess.run(None, {name: batch})  # warm-up: memory arenas and kernel selection

    cpu_begin = cpu_seconds()
    wall_begin = time.perf_counter()
    for _ in range(inferences):
        logits = sess.run(None, {name: batch})[0]
    wall = time.perf_counter() - wall_begin
    cpu = cpu_seconds() - cpu_begin

    del sess
    gc.collect()
    return {
        "threads": threads,
        "pool_threads": pool,
        "inferences": inferences,
        "wall_seconds": wall,
        "cpu_seconds": cpu,
        "seconds_per_inference": wall / inferences,
        "top1": int(np.argmax(logits)),
        # Summation order depends on the thread count, hence the rounding.
        "checksum": round(float(logits.sum()), 3),
    }


def idle(seconds):
    """Pause between runs; CPU time spent here belongs to workers that are still spinning."""
    cpu_begin = cpu_seconds()
    time.sleep(seconds)
    return cpu_seconds() - cpu_begin


def handler(event):
    inferences = event["inferences"]
    idle_seconds = event.get("idle_seconds", 0.0)
    policy = event.get("omp_wait_policy")
    if policy not in SPINNING:
        raise ValueError(f"Unknown wait policy {policy}, expected active or passive")
    spinning = SPINNING[policy]
    order = event.get("order", "ascending")
    seed = event.get("seed", random.randrange(2**32))

    thread_counts = list(event.get("threads") or [1, 2, 3, 4, 6, 8])
    if order == "shuffled":
        random.Random(seed).shuffle(thread_counts)
    elif order != "ascending":
        raise ValueError(f"Unknown order {order}, expected ascending or shuffled")

    # A fixed input: the image decoding of 411.image-recognition does not involve the thread pool.
    batch = np.random.default_rng(0).standard_normal((1, 3, 224, 224), dtype=np.float32)

    # What an application gets without setting the thread count.
    default_run = run_inference(0, spinning, batch, inferences)
    default_run["idle_cpu_seconds_after"] = idle(idle_seconds)

    environment = {
        "os_cpu_count": os.cpu_count(),
        "sched_affinity": len(os.sched_getaffinity(0)),
        "cpus_allowed_list": status_field("Cpus_allowed_list"),
        "onnxruntime_version": ort.__version__,
        "onnxruntime_default_threads": default_run["pool_threads"],
        "omp_wait_policy": policy,
        "allow_spinning": spinning,
        "cpu_model": cpu_model(),
        "cgroup": {path: read_file(path) for path in CGROUP_FILES if os.path.exists(path)},
    }

    runs = []
    for position, threads in enumerate(thread_counts):
        run = run_inference(threads, spinning, batch, inferences)
        run["position"] = position
        run["idle_cpu_seconds_after"] = idle(idle_seconds)
        runs.append(run)

    return {
        "result": {
            "environment": environment,
            "default_run": default_run,
            "runs": runs,
            "order": order,
            "seed": seed,
        },
        "measurement": {"inferences": inferences, "idle_seconds": idle_seconds},
    }
