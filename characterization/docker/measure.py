#!/usr/bin/env python3
"""Measure one SeBS function inside the characterization container.

PAPI's libpfm has no PMU for this hybrid CPU, so the hardware counts come from
``perf stat`` on the ``cpu_core`` PMU. The CSV columns still use the PAPI names
``characterization.ingest`` expects. Each group is a separate process. Counters
are enabled only around the handler, after imports, and are checked to have
run for the whole interval.

Memory, disk, and CPU time come from a separate uninstrumented run.
"""

from __future__ import annotations

import csv
import datetime
import gc
import json
import os
import resource
import signal
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

# perf event, then the weight used when several events form one PAPI column.
# SP/DP weights are vector widths so the column is a flop count.
GROUPS: dict[str, dict[str, list[tuple[str, int]]]] = {
    "cycles_papi": {
        "PAPI_TOT_CYC": [("cycles", 1)],
        "PAPI_TOT_INS": [("instructions", 1)],
        "PAPI_STL_ICY": [("cycle_activity.stalls_total", 1)],
    },
    "inscount_papi": {
        "PAPI_TOT_INS": [("instructions", 1)],
        "PAPI_LST_INS": [
            ("mem_inst_retired.all_loads", 1),
            ("mem_inst_retired.all_stores", 1),
        ],
        "PAPI_BR_INS": [("branch-instructions", 1)],
    },
    "cache_papi": {
        "PAPI_TOT_INS": [("instructions", 1)],
        "PAPI_L1_DCM": [("mem_load_retired.l1_miss", 1)],
        "PAPI_L2_TCM": [("mem_load_retired.l2_miss", 1)],
        "PAPI_L3_TCM": [("mem_load_retired.l3_miss", 1)],
    },
    "sp_flops_papi": {
        "PAPI_TOT_INS": [("instructions", 1)],
        "PAPI_SP_OPS": [
            ("fp_arith_inst_retired.scalar_single", 1),
            ("fp_arith_inst_retired.128b_packed_single", 4),
            ("fp_arith_inst_retired.256b_packed_single", 8),
        ],
    },
    "dp_flops_papi": {
        "PAPI_TOT_INS": [("instructions", 1)],
        "PAPI_DP_OPS": [
            ("fp_arith_inst_retired.scalar_double", 1),
            ("fp_arith_inst_retired.128b_packed_double", 4),
            ("fp_arith_inst_retired.256b_packed_double", 8),
        ],
    },
}

RESULTS = Path("/results")
DISK_HEADER = [
    "Timestamp",
    "WriteChars",
    "ReadChars",
    "ReadSysCalls",
    "WriteSysCalls",
    "ReadBytes",
    "WriteBytes",
]


def stamp() -> str:
    return datetime.datetime.now().strftime("%s.%f")


def write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def read_io(pid: int) -> dict[str, int] | None:
    try:
        lines = Path(f"/proc/{pid}/io").read_text().splitlines()
    except OSError:
        return None
    fields: dict[str, int] = {}
    for line in lines:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = int(value.strip())
    return fields


def read_rollup(pid: int) -> tuple[int, int, int] | None:
    try:
        lines = Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines()
    except OSError:
        return None
    fields: dict[str, int] = {}
    for line in lines:
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        fields[key.strip()] = int(rest.strip().split()[0])
    uss = fields.get("Private_Clean", 0) + fields.get("Private_Dirty", 0)
    return uss, fields.get("Pss", 0), fields.get("Rss", 0)


def process_tree(root: int) -> list[int]:
    parents: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            text = (entry / "status").read_text()
        except OSError:
            continue
        for line in text.splitlines():
            if line.startswith("PPid:"):
                parents[int(entry.name)] = int(line.split()[1])
                break
    found = [root]
    growing = True
    while growing:
        growing = False
        for pid, parent in parents.items():
            if parent in found and pid not in found:
                found.append(pid)
                growing = True
    return found


def sample_memory() -> tuple[int, int, int]:
    uss = pss = rss = 0
    for pid in process_tree(os.getpid()):
        rolled = read_rollup(pid)
        if rolled is None:
            continue
        uss += rolled[0]
        pss += rolled[1]
        rss += rolled[2]
    return uss, pss, rss


def memory_sampler(stop: threading.Event, rows: list[list], lock: threading.Lock) -> None:
    while not stop.is_set():
        uss, pss, rss = sample_memory()
        with lock:
            rows.append([stamp(), 1, 0, uss, pss, rss])
        stop.wait(0.02)


def import_handler():
    sys.path.insert(0, "/work")
    from function.function import handler

    return handler


def group_events(name: str) -> list[str]:
    events: list[str] = []
    for terms in GROUPS[name].values():
        for event, _weight in terms:
            if event not in events:
                events.append(event)
    return events


def parse_perf(path: Path) -> dict[str, tuple[float, float]]:
    """Map a cpu_core event name to ``(count, percent running)``."""
    found: dict[str, tuple[float, float]] = {}
    for line in path.read_text().splitlines():
        if not line or line.startswith("#") or line.startswith("Events "):
            continue
        parts = line.split(";")
        if len(parts) < 3:
            continue
        raw, event = parts[0], parts[2]
        if "cpu_atom/" in event or raw in {"<not counted>", "<not supported>", ""}:
            continue
        percent = float(parts[4]) if len(parts) > 4 and parts[4] else 100.0
        found[event] = (float(raw), percent)
    return found


def column_values(found: dict[str, tuple[float, float]], name: str) -> tuple[dict[str, float], list[str]]:
    values: dict[str, float] = {}
    notes: list[str] = []
    for column, terms in GROUPS[name].items():
        total = 0.0
        for event, weight in terms:
            matches = [
                item
                for key, item in found.items()
                if key.endswith(f"/{event}/") or key == event
            ]
            if not matches:
                notes.append(f"missing {event}")
                total = -1
                break
            count, percent = matches[0]
            if percent < 99.0:
                notes.append(f"{event} ran {percent:.2f}% (multiplexed)")
                total = -1
                break
            total += count * weight
        if total >= 0:
            values[column] = total
    return values, notes


def run_calls(handler, payload: dict, repetitions: int) -> None:
    for _ in range(repetitions):
        gc.collect()
        gc.disable()
        try:
            handler(payload)
        finally:
            gc.enable()


def run_perf_group(cfg: dict, name: str) -> None:
    handler = import_handler()
    payload = cfg["input"]
    repetitions = int(cfg["repetitions"])
    go_read, go_write = os.pipe()
    worker = os.fork()
    if worker == 0:
        os.close(go_write)
        os.read(go_read, 1)
        try:
            run_calls(handler, payload, repetitions)
        except Exception:
            traceback.print_exc()
            os._exit(1)
        os._exit(0)

    os.close(go_read)
    control_read, control_write = os.pipe()
    perf_path = RESULTS / "papi" / f"{name}.perf.txt"
    perf_path.parent.mkdir(parents=True, exist_ok=True)
    perf = subprocess.Popen(
        [
            "perf",
            "stat",
            "-D",
            "-1",
            "--control",
            f"fd:{control_read}",
            "-p",
            str(worker),
            "-x",
            ";",
            "-e",
            ",".join(group_events(name)),
            "-o",
            str(perf_path),
        ],
        pass_fds=(control_read,),
        stderr=subprocess.PIPE,
        text=True,
    )
    os.close(control_read)
    time.sleep(0.15)
    if perf.poll() is not None:
        os.close(go_write)
        os.kill(worker, signal.SIGKILL)
        os.waitpid(worker, 0)
        raise RuntimeError(f"perf exited early: {perf.stderr.read() if perf.stderr else ''}")
    os.write(control_write, b"enable\n")
    os.write(go_write, b"G")
    os.close(go_write)
    _pid, status = os.waitpid(worker, 0)
    os.write(control_write, b"disable\n")
    os.close(control_write)
    time.sleep(0.05)
    perf.send_signal(signal.SIGINT)
    perf.wait(timeout=15)
    code = os.waitstatus_to_exitcode(status)
    if code != 0:
        raise SystemExit(code)
    found = parse_perf(perf_path)
    values, notes = column_values(found, name)
    meta = {"set": name, "columns": values, "notes": notes, "perf_events": group_events(name)}
    (RESULTS / "papi" / f"{name}.events.json").write_text(json.dumps(meta, indent=2) + "\n")
    if "PAPI_TOT_INS" not in values and "PAPI_TOT_CYC" not in values:
        raise SystemExit(2)
    if notes:
        print(f"{name} partial: {notes}", flush=True)
    header = ["Time", "RelativeTime", *values.keys()]
    write_csv(RESULTS / "papi" / f"{name}.csv", header, [[stamp(), 0, *values.values()]])
    print(f"{name} ok {values}", flush=True)


def run_uninstrumented(cfg: dict) -> None:
    handler = import_handler()
    payload = cfg["input"]
    repetitions = int(cfg["repetitions"])
    rows: list[list] = []
    lock = threading.Lock()
    stop = threading.Event()
    thread = threading.Thread(target=memory_sampler, args=(stop, rows, lock), daemon=True)
    io_before = read_io(os.getpid())
    thread.start()
    time_rows = []
    try:
        for _ in range(repetitions):
            gc.collect()
            gc.disable()
            begin = datetime.datetime.now()
            before = resource.getrusage(resource.RUSAGE_SELF)
            before_children = resource.getrusage(resource.RUSAGE_CHILDREN)
            try:
                handler(payload)
            finally:
                after = resource.getrusage(resource.RUSAGE_SELF)
                after_children = resource.getrusage(resource.RUSAGE_CHILDREN)
                end = datetime.datetime.now()
                gc.enable()
            user = (after.ru_utime - before.ru_utime) + (
                after_children.ru_utime - before_children.ru_utime
            )
            system = (after.ru_stime - before.ru_stime) + (
                after_children.ru_stime - before_children.ru_stime
            )
            time_rows.append(
                [
                    begin.strftime("%s.%f"),
                    end.strftime("%s.%f"),
                    (end - begin) / datetime.timedelta(microseconds=1),
                    user * 1e6,
                    system * 1e6,
                ]
            )
    finally:
        stop.set()
        thread.join(timeout=2)
        io_after = read_io(os.getpid())
        with lock:
            uss, pss, rss = sample_memory()
            rows.append([stamp(), 1, 0, uss, pss, rss])
    write_csv(RESULTS / "time" / "time.csv", ["Begin", "End", "Duration", "User", "Sys"], time_rows)
    write_csv(RESULTS / "memory" / "memory.csv", ["Timestamp", "N", "Cached", "USS", "PSS", "RSS"], rows)
    if io_before and io_after:
        disk_rows = []
        for sample in (io_before, io_after):
            disk_rows.append(
                [
                    stamp(),
                    1,
                    sample.get("rchar", 0),
                    sample.get("wchar", 0),
                    sample.get("syscr", 0),
                    sample.get("syscw", 0),
                    sample.get("read_bytes", 0),
                    sample.get("write_bytes", 0),
                ]
            )
        write_csv(RESULTS / "disk-io" / "disk-io.csv", DISK_HEADER, disk_rows)
    print(f"uninstrumented ok wall_us={time_rows[-1][2]:.0f}", flush=True)


def run_probe() -> None:
    perf_path = RESULTS / "probe-perf.txt"
    loop = "s=0\nfor i in range(1000000):\n s+=i*i\nprint(s)\n"
    subprocess.run(
        ["perf", "stat", "-x", ";", "-e", "cycles,instructions", "-o", str(perf_path), "--", "python3", "-c", loop],
        check=False,
    )
    found = parse_perf(perf_path)
    cycles = next((item[0] for key, item in found.items() if key.endswith("/cycles/")), 0)
    instructions = next((item[0] for key, item in found.items() if key.endswith("/instructions/")), 0)
    payload = {"ok": cycles > 0 and instructions > 0, "counts": {"PAPI_TOT_CYC": cycles, "PAPI_TOT_INS": instructions}}
    (RESULTS / "probe.json").write_text(json.dumps(payload) + "\n")
    print(json.dumps(payload), flush=True)
    if not payload["ok"]:
        raise SystemExit(2)


def mode_complete(name: str) -> bool:
    if name == "uninstrumented":
        path = RESULTS / "time" / "time.csv"
    else:
        path = RESULTS / "papi" / f"{name}.csv"
    return path.is_file() and path.stat().st_size > 0


def run_mode(cfg: dict, mode: str) -> None:
    try:
        if mode == "uninstrumented":
            run_uninstrumented(cfg)
        elif mode == "probe":
            run_probe()
        else:
            run_perf_group(cfg, mode)
    except SystemExit:
        raise
    except Exception:
        RESULTS.mkdir(parents=True, exist_ok=True)
        (RESULTS / f"error-{mode}.txt").write_text(traceback.format_exc())
        traceback.print_exc()
        raise SystemExit(1)


def release_storage(cfg: dict) -> None:
    """Drop objects written by this pass so the next pass can reuse the disk.

    The large uploader keeps a distinct object per call. MinIO refuses writes
    once the host has little free space, so a finished pass must not retain them.
    """
    bucket_cfg = (cfg.get("input") or {}).get("bucket") or {}
    bucket = bucket_cfg.get("bucket")
    prefix = bucket_cfg.get("output")
    address = os.environ.get("MINIO_ADDRESS")
    if not bucket or not prefix or not address:
        return
    try:
        from minio import Minio

        client = Minio(
            address,
            access_key=os.environ["MINIO_ACCESS_KEY"],
            secret_key=os.environ["MINIO_SECRET_KEY"],
            secure=False,
        )
        for item in client.list_objects(bucket, prefix=prefix, recursive=True):
            client.remove_object(bucket, item.object_name)
    except Exception as exc:
        print(f"storage cleanup skipped: {exc}", flush=True)


def main() -> None:
    cfg = json.loads(Path(sys.argv[1]).read_text())
    RESULTS.mkdir(parents=True, exist_ok=True)
    mode = os.environ.get("SEBS_MEASURE_MODE", "")
    if cfg.get("probe"):
        run_mode(cfg, "probe")
        return
    if mode:
        run_mode(cfg, mode)
        return
    status = {}
    for name in ["uninstrumented", *GROUPS]:
        if mode_complete(name):
            status[name] = 0
            print(f"mode {name} already complete", flush=True)
            continue
        env = os.environ.copy()
        env["SEBS_MEASURE_MODE"] = name
        completed = subprocess.run([sys.executable, __file__, sys.argv[1]], env=env)
        status[name] = completed.returncode
        print(f"mode {name} exit {completed.returncode}", flush=True)
        release_storage(cfg)
    (RESULTS / "status.json").write_text(json.dumps(status, indent=2) + "\n")
    papi_ok = all(status.get(name, 1) == 0 for name in GROUPS)
    if status.get("uninstrumented", 1) != 0 or not papi_ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
