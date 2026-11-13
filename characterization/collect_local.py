#!/usr/bin/env python3
"""Collect a local hardware-counter sample for the characterization suite.

Builds ``sebs-char-measure:local`` if needed, then runs each Python benchmark
in a privileged container pinned to one core. The container writes the tree
``characterization.ingest`` already reads.

Input size is ``small``. The default is the short sample, one or three handler
repetitions. ``--repetitions 30`` is the reported local sample.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from characterization.suite import SUITE

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "sebs-char-measure:local"
DOCKERFILE = ROOT / "characterization" / "docker" / "Dockerfile"
MEASURE = ROOT / "characterization" / "docker" / "measure.py"
PAPI_SETS = (
    "inscount_papi",
    "sp_flops_papi",
    "dp_flops_papi",
    "cache_papi",
    "cycles_papi",
)
BUCKET = "sebs"
MINIO_ACCESS = "sebsaccess"
MINIO_SECRET = "sebssecret"
MINIO_PORT = 19000
DYNAMO_PORT = 18000
# Digests from the 2026-10-17 initial sample. :latest is not a stable name.
MINIO_IMAGE = "pgsty/silo@sha256:aaac6bce13eb99316b3c88bd20143fc3c38ab607585256a411a095c44ee57f62"
DYNAMO_IMAGE = "amazon/dynamodb-local@sha256:6ada631ec7aa5fc83446855eb17233e3dad184fec47477e8140c6c569cfe0c86"

# Distribution names whose installed size is part of the deployment artifact.
DEP_SEEDS = {
    "010.sleep": [],
    "110.dynamic-html": ["jinja2"],
    "120.uploader": ["minio"],
    "130.crud-api": ["boto3"],
    "210.thumbnailer": ["pillow", "minio"],
    "220.video-processing": ["minio"],
    "311.compression": ["minio"],
    "411.image-recognition": ["torch", "torchvision", "pillow", "numpy", "minio"],
    "501.graph-pagerank": ["igraph"],
    "502.graph-mst": ["igraph"],
    "503.graph-bfs": ["igraph"],
    "504.dna-visualisation": ["squiggle", "minio"],
}

LONG_BENCHMARKS = {
    "010.sleep",
    "120.uploader",
    "220.video-processing",
    "311.compression",
    "411.image-recognition",
    "504.dna-visualisation",
}


def docker_argv(args: list[str]) -> list[str]:
    import shlex

    inner = " ".join(shlex.quote(part) for part in ["docker", *args])
    return ["sg", "docker", "-c", inner]


def run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(docker_argv(args), **kwargs)


def require_docker() -> None:
    probe = run(["info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True)
    if probe.returncode != 0:
        raise SystemExit(
            "Docker is not reachable. Open a shell with `newgrp docker` or log in again "
            "so the docker group applies."
        )


def image_present() -> bool:
    probe = run(["image", "inspect", IMAGE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return probe.returncode == 0


def build_image() -> None:
    context = Path("/tmp/sebs-char-image")
    if context.exists():
        shutil.rmtree(context)
    context.mkdir(parents=True)
    shutil.copy(DOCKERFILE, context / "Dockerfile")
    shutil.copy(MEASURE, context / "measure.py")
    archive = subprocess.run(
        ["tar", "-C", str(ROOT / "third-party"), "--exclude", ".git", "-cf", "-", "pypapi"],
        check=True,
        capture_output=True,
    )
    subprocess.run(["tar", "-C", str(context), "-xf", "-"], input=archive.stdout, check=True)
    print(f"Building {IMAGE}", flush=True)
    subprocess.run(docker_argv(["build", "-t", IMAGE, str(context)]), check=True)


def pick_cpu() -> str:
    text = subprocess.check_output(["lscpu", "-e=CPU,MAXMHZ"], text=True)
    best: tuple[float, int] | None = None
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        cpu = int(parts[0])
        mhz = float(parts[1])
        if best is None or mhz > best[0] or (mhz == best[0] and cpu < best[1]):
            best = (mhz, cpu)
    return str(best[1] if best else 0)


def find_dir(root: Path, name: str) -> Path | None:
    if not root.is_dir():
        return None
    matches = [path for path in root.rglob(name) if path.is_dir() and path.name == name]
    return matches[0] if matches else None


def directory_bytes(path: Path) -> int:
    total = 0
    for file in path.rglob("*"):
        if file.is_file() and "__pycache__" not in file.parts:
            total += file.stat().st_size
    return total


def load_input_module(code_dir: Path):
    path = code_dir / "input.py"
    spec = importlib.util.spec_from_file_location(f"sebs_input_{code_dir.name.replace('.', '_')}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def wait_port(port: int, timeout: float = 90) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as sock:
            sock.settimeout(1)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.4)
    raise TimeoutError(f"Nothing accepted connections on 127.0.0.1:{port}")


def container_ip(name: str) -> str:
    probe = run(
        ["inspect", "-f", "{{range.NetworkSettings.Networks}}{{.IPAddress}}{{end}}", name],
        capture_output=True,
        text=True,
        check=True,
    )
    address = probe.stdout.strip()
    if not address:
        raise RuntimeError(f"Container {name} has no IP address")
    return address


def chown_tree(path: Path) -> None:
    if not path.exists() or not image_present():
        return
    run(
        [
            "run",
            "--rm",
            "--entrypoint",
            "chown",
            "-v",
            f"{path.resolve()}:/data",
            IMAGE,
            "-R",
            f"{os.getuid()}:{os.getgid()}",
            "/data",
        ],
        check=True,
    )


def stream(args: list[str], timeout: float, log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w") as log:
        process = subprocess.Popen(
            docker_argv(args),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert process.stdout is not None

        def watchdog() -> None:
            time.sleep(timeout)
            if process.poll() is None:
                process.kill()
                run(["rm", "-f", "sebs-char-run"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        threading.Thread(target=watchdog, daemon=True).start()
        try:
            for line in process.stdout:
                sys.stdout.write(line)
                log.write(line)
            code = process.wait()
            if code < 0:
                log.write("\nTimed out or killed\n")
                return 124
            return code
        except BaseException:
            process.kill()
            run(["rm", "-f", "sebs-char-run"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            raise


class Services:
    def __init__(self) -> None:
        self.minio = None
        self.dynamo = None
        self.minio_ip = ""
        self.dynamo_ip = ""

    def ensure_minio(self):
        if self.minio is not None:
            return self.minio
        run(["rm", "-f", "sebs-char-minio"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        run(
            [
                "run",
                "-d",
                "--name",
                "sebs-char-minio",
                "-p",
                f"{MINIO_PORT}:9000",
                "-e",
                f"MINIO_ROOT_USER={MINIO_ACCESS}",
                "-e",
                f"MINIO_ROOT_PASSWORD={MINIO_SECRET}",
                MINIO_IMAGE,
                "server",
                "/data",
                "--console-address",
                ":9001",
            ],
            check=True,
        )
        wait_port(MINIO_PORT)
        from minio import Minio

        client = Minio(
            f"127.0.0.1:{MINIO_PORT}",
            access_key=MINIO_ACCESS,
            secret_key=MINIO_SECRET,
            secure=False,
        )
        deadline = time.time() + 60
        while True:
            try:
                if not client.bucket_exists(BUCKET):
                    client.make_bucket(BUCKET)
                break
            except Exception:
                if time.time() > deadline:
                    raise
                time.sleep(0.5)
        self.minio = client
        self.minio_ip = container_ip("sebs-char-minio")
        print(f"MinIO at {self.minio_ip}:9000", flush=True)
        return client

    def ensure_dynamo(self):
        if self.dynamo is not None:
            return self.dynamo
        run(["rm", "-f", "sebs-char-dynamodb"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        run(
            [
                "run",
                "-d",
                "--name",
                "sebs-char-dynamodb",
                "-p",
                f"{DYNAMO_PORT}:8000",
                DYNAMO_IMAGE,
                "-jar",
                "DynamoDBLocal.jar",
                "-inMemory",
                "-sharedDb",
            ],
            check=True,
        )
        wait_port(DYNAMO_PORT)
        import boto3
        from botocore.exceptions import ClientError

        endpoint = f"http://127.0.0.1:{DYNAMO_PORT}"
        client = boto3.client(
            "dynamodb",
            endpoint_url=endpoint,
            region_name="us-east-1",
            aws_access_key_id="None",
            aws_secret_access_key="None",
        )
        deadline = time.time() + 60
        while True:
            try:
                client.create_table(
                    TableName="shopping_cart",
                    KeySchema=[
                        {"AttributeName": "cart_id", "KeyType": "HASH"},
                        {"AttributeName": "product_id", "KeyType": "RANGE"},
                    ],
                    AttributeDefinitions=[
                        {"AttributeName": "cart_id", "AttributeType": "S"},
                        {"AttributeName": "product_id", "AttributeType": "S"},
                    ],
                    BillingMode="PAY_PER_REQUEST",
                )
                break
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code == "ResourceInUseException":
                    break
                raise
            except Exception:
                if time.time() > deadline:
                    raise
                time.sleep(0.5)
        self.dynamo = boto3.resource(
            "dynamodb",
            endpoint_url=endpoint,
            region_name="us-east-1",
            aws_access_key_id="None",
            aws_secret_access_key="None",
        ).Table("shopping_cart")
        self.dynamo_ip = container_ip("sebs-char-dynamodb")
        print(f"DynamoDB Local at {self.dynamo_ip}:8000", flush=True)
        return self.dynamo

    def stop(self) -> None:
        run(["rm", "-f", "sebs-char-minio", "sebs-char-dynamodb"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def dependency_catalog() -> dict:
    script = Path("/tmp/sebs-char-deps.py")
    script.write_text(
        "import importlib.metadata as md, json\n"
        "out = {}\n"
        "for dist in md.distributions():\n"
        "    name = (dist.metadata['Name'] or '').lower()\n"
        "    total = 0\n"
        "    for item in dist.files or []:\n"
        "        try:\n"
        "            located = dist.locate_file(item)\n"
        "            if located.is_file() and hasattr(located, 'stat'):\n"
        "                total += located.stat().st_size\n"
        "        except OSError:\n"
        "            pass\n"
        "    reqs = []\n"
        "    for req in dist.requires or []:\n"
        "        reqs.append(req.split(';')[0].split()[0].split('[')[0])\n"
        "    out[name] = {'bytes': total, 'requires': reqs}\n"
        "print(json.dumps(out))\n"
    )
    probe = run(
        [
            "run",
            "--rm",
            "--entrypoint",
            "python",
            "-v",
            f"{script}:/tmp/sebs-char-deps.py:ro",
            IMAGE,
            "/tmp/sebs-char-deps.py",
        ],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        raise RuntimeError(probe.stderr or probe.stdout)
    return json.loads(probe.stdout)


def closure_bytes(catalog: dict, seeds: list[str]) -> int:
    seen: set[str] = set()
    stack = [seed.lower() for seed in seeds]
    total = 0
    while stack:
        name = stack.pop()
        if name in seen or name not in catalog:
            continue
        seen.add(name)
        total += int(catalog[name]["bytes"])
        for requirement in catalog[name]["requires"]:
            stack.append(requirement.lower())
    return total


def repetitions_for(name: str) -> int:
    return 1 if name in LONG_BENCHMARKS else 3


def timeout_for(name: str, repetitions: int, size: str = "small") -> float:
    """Wall-clock budget for six passes: five counter groups plus one plain run.

    ``large`` sleep is 280 seconds per call, under OpenWhisk's 5-minute cap.
    The small-input budget would kill that run after the first pass.
    """
    if name == "010.sleep":
        seconds = {"test": 1, "small": 100, "large": 280}[size]
        return 180 + repetitions * 6 * (seconds + 20)
    per_call = 10
    if name in {"411.image-recognition", "220.video-processing"}:
        per_call = 90 if size == "large" else 30
    elif name == "504.dna-visualisation":
        per_call = 20
    elif size == "large" and name in {
        "120.uploader",
        "501.graph-pagerank",
        "502.graph-mst",
        "503.graph-bfs",
    }:
        per_call = 60
    floor = 1800 if name in {"411.image-recognition", "220.video-processing"} else 900
    return max(floor, 180 + repetitions * 6 * per_call)


def stage_function(name: str, code_dir: Path, build_root: Path) -> Path:
    staged = build_root / name
    if staged.exists():
        shutil.rmtree(staged)
    function_dir = staged / "function"
    shutil.copytree(
        code_dir / "python",
        function_dir,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    resources = code_dir / "resources"
    if resources.is_dir():
        shutil.copytree(resources, function_dir / "resources", dirs_exist_ok=True)
    wrappers = ROOT / "benchmarks" / "wrappers" / "local" / "python"
    for wrapper in ("storage.py", "nosql.py"):
        shutil.copy2(wrappers / wrapper, function_dir / wrapper)
    storage = (function_dir / "storage.py").read_text()
    needle = "def download(self, bucket, file, filepath):\n        self.client.fget_object(bucket, file, filepath)"
    replacement = (
        "def download(self, bucket, file, filepath):\n"
        "        os.makedirs(os.path.dirname(filepath) or '.', exist_ok=True)\n"
        "        self.client.fget_object(bucket, file, filepath)"
    )
    if needle in storage:
        storage = storage.replace(needle, replacement, 1)
    # Current MinIO SDK dropped list_objects_v2; list_objects is the same call.
    storage = storage.replace(
        "self.client.list_objects_v2(bucket, prefix, recursive=True)",
        "self.client.list_objects(bucket, prefix=prefix, recursive=True)",
    )
    (function_dir / "storage.py").write_text(storage)
    for script_dir in (code_dir / "python", code_dir):
        script = script_dir / "init.sh"
        if script.is_file():
            subprocess.run(
                ["bash", str(script), str(function_dir), "false", "x64"],
                check=True,
            )
    init_file = function_dir / "__init__.py"
    if not init_file.exists():
        init_file.write_text("")
    return staged


def prepare_input(name: str, size: str, module, services: Services) -> tuple[dict, bool, bool]:
    n_in, n_out = (0, 0)
    if hasattr(module, "buckets_count"):
        n_in, n_out = module.buckets_count()
    uses_storage = (n_in + n_out) > 0
    uses_nosql = hasattr(module, "allocate_nosql")
    inputs = [f"{name}-{index}-input" for index in range(n_in)]
    outputs = [f"{name}-{index}-output" for index in range(n_out)]
    upload = None
    if uses_storage:
        client = services.ensure_minio()

        def upload(idx, key, filepath, inputs=inputs, client=client):
            client.fput_object(BUCKET, f"{inputs[idx]}/{key}", str(filepath))

    nosql_upload = None
    if uses_nosql:
        table = services.ensure_dynamo()

        def nosql_upload(benchmark, table_name, data, primary, secondary, table=table):
            item = dict(data)
            item[primary[0]] = primary[1]
            if secondary:
                item[secondary[0]] = secondary[1]
            table.put_item(Item=item)

    data_dir = find_dir(ROOT / "benchmarks-data", name)
    payload = module.generate_input(
        str(data_dir) if data_dir else None,
        size,
        BUCKET if uses_storage else None,
        inputs,
        outputs,
        upload,
        nosql_upload,
    )
    return payload, uses_storage, uses_nosql


def run_container(
    staged: Path,
    output: Path,
    cpu: str,
    timeout: float,
    services: Services,
    uses_storage: bool,
    uses_nosql: bool,
) -> int:
    env: list[str] = []
    if uses_storage:
        env += [
            "-e",
            f"MINIO_ADDRESS={services.minio_ip}:9000",
            "-e",
            f"MINIO_ACCESS_KEY={MINIO_ACCESS}",
            "-e",
            f"MINIO_SECRET_KEY={MINIO_SECRET}",
        ]
    if uses_nosql:
        env += [
            "-e",
            "NOSQL_STORAGE_TYPE=scylladb",
            "-e",
            f"NOSQL_STORAGE_ENDPOINT={services.dynamo_ip}:8000",
            "-e",
            "NOSQL_STORAGE_TABLE_shopping_cart=shopping_cart",
        ]
    args = [
        "run",
        "--rm",
        "--name",
        "sebs-char-run",
        "--privileged",
        "--security-opt",
        "seccomp=unconfined",
        "--cpuset-cpus",
        cpu,
        "--memory",
        "2g",
        "--network",
        "bridge",
        "-v",
        f"{staged.resolve()}:/work:ro",
        "-v",
        f"{output.resolve()}:/results",
        "-v",
        f"{(DOCKERFILE.parent / 'measure.py').resolve()}:/opt/measure.py:ro",
        *env,
        IMAGE,
        "/work/job.json",
    ]
    return stream(args, timeout, output / "container.log")


def probe_papi(results: Path, cpu: str) -> dict:
    output = results.parent / "probe"
    if output.exists():
        chown_tree(output)
        shutil.rmtree(output)
    output.mkdir(parents=True)
    job = output / "job.json"
    job.write_text(json.dumps({"probe": True}) + "\n")
    code = stream(
        [
            "run",
            "--rm",
            "--name",
            "sebs-char-run",
            "--privileged",
            "--security-opt",
            "seccomp=unconfined",
            "--cpuset-cpus",
            cpu,
            "-v",
            f"{job.resolve()}:/work/job.json:ro",
            "-v",
            f"{output.resolve()}:/results",
            IMAGE,
            "/work/job.json",
        ],
        180,
        output / "container.log",
    )
    chown_tree(output)
    payload = json.loads((output / "probe.json").read_text()) if (output / "probe.json").is_file() else {}
    if code != 0 or not payload.get("ok"):
        raise SystemExit(f"PAPI probe failed: {json.dumps(payload)}")
    print(
        f"PAPI probe ok  instructions={payload['counts'].get('PAPI_TOT_INS')} "
        f"cycles={payload['counts'].get('PAPI_TOT_CYC')}",
        flush=True,
    )
    return payload


def host_record(cpu: str) -> dict:
    model = ""
    for line in Path("/proc/cpuinfo").read_text().splitlines():
        if line.startswith("model name"):
            model = line.split(":", 1)[1].strip()
            break
    paranoid = int(Path("/proc/sys/kernel/perf_event_paranoid").read_text().strip())
    governor_path = Path(f"/sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_governor")
    governor = governor_path.read_text().strip() if governor_path.is_file() else ""
    try:
        sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True).strip()
    except subprocess.CalledProcessError:
        sha = ""
    return {
        "cpu_model": model,
        "pinned_cpu": int(cpu),
        "scaling_governor": governor,
        "perf_event_paranoid": paranoid,
        "git": sha,
        "uname": os.uname().machine,
    }


def write_manifest(
    results: Path,
    cpu: str,
    repetitions: dict[str, int],
    succeeded: list[str],
    size: str = "small",
) -> None:
    manifest_path = results / "manifest.json"
    manifest = {}
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text())
    platforms = set(manifest.get("platforms", []))
    platforms.add("local")
    prior = [name for name in manifest.get("succeeded", []) if name not in repetitions]
    merged = prior + [name for name in succeeded if name not in prior]
    prior_reps = dict(manifest.get("repetitions") or {})
    prior_reps.update(repetitions)
    image_id = run(
        ["image", "inspect", "-f", "{{.Id}}", IMAGE],
        capture_output=True,
        text=True,
    )
    manifest.update(
        {
            "language": "python",
            "input_size": size,
            "origin": "measured",
            "platforms": sorted(platforms),
            "container_memory": "2g",
            "repetitions": prior_reps,
            "succeeded": merged,
            "images": {
                "measure": IMAGE,
                "measure_id": image_id.stdout.strip() if image_id.returncode == 0 else "",
                "minio": MINIO_IMAGE,
                "dynamodb": DYNAMO_IMAGE,
            },
            "method": (
                "Privileged container pinned to one P-core. PAPI 7 and the bundled PAPI 5.5 "
                "report no PMU on this hybrid CPU (libpfm: no default PMU), so counts come from "
                "perf stat on cpu_core. Columns keep the PAPI names ingest expects: "
                "cycles, instructions, cycle_activity.stalls_total as PAPI_STL_ICY, "
                "mem_inst_retired loads+stores as PAPI_LST_INS, branch-instructions as PAPI_BR_INS, "
                "mem_load_retired l1/l2/l3 misses, and width-weighted fp_arith_inst_retired as SP/DP ops. "
                "Groups are separate runs and were checked to run at 100% (no multiplexing). "
                "Time, peak memory including child processes, and /proc/self/io come from an "
                "uninstrumented run. CPU time includes reaped children. "
                + (
                    f"Handler repetitions: {min(repetitions.values())}."
                    if repetitions
                    else "Handler repetitions were not recorded."
                )
            ),
            "host": host_record(cpu),
        }
    )
    if size == "large":
        manifest["size_invariant_benchmarks"] = [
            "210.thumbnailer",
            "311.compression",
            "411.image-recognition",
            "504.dna-visualisation",
        ]
        manifest["platform_limits"] = {
            "010.sleep": (
                "The large sleep is 280s inside a 300s timeout, under this OpenWhisk "
                "cluster's 5-minute action cap."
            )
        }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")


def benchmark_complete(output: Path) -> bool:
    """A benchmark is recorded only when every counter group wrote its CSV."""
    if not (output / "package.json").is_file():
        return False
    status_path = output / "status.json"
    if not status_path.is_file():
        return False
    status = json.loads(status_path.read_text())
    return status.get("uninstrumented") == 0 and all(status.get(event) == 0 for event in PAPI_SETS)


def collect(
    benchmarks: list[str],
    results: Path,
    size: str,
    cpu: str,
    repetition_override: int | None = None,
    resume: bool = False,
) -> list[str]:
    build_root = results.parent / "build"
    failed_root = results.parent / "failed"
    build_root.mkdir(parents=True, exist_ok=True)
    catalog = dependency_catalog()
    services = Services()
    succeeded: list[str] = []
    repetitions: dict[str, int] = {}
    try:
        for name in benchmarks:
            print(f"\n=== {name} ===", flush=True)
            code_dir = find_dir(ROOT / "benchmarks", name)
            if code_dir is None:
                print(f"Missing benchmark directory for {name}", flush=True)
                continue
            reps = (
                repetition_override
                if repetition_override is not None
                else repetitions_for(name)
            )
            repetitions[name] = reps
            output = results / "local" / name
            if resume and benchmark_complete(output):
                repetitions[name] = reps
                succeeded.append(name)
                print(f"{name} already recorded, skipping", flush=True)
                continue
            try:
                if output.exists() and not resume:
                    chown_tree(output)
                    shutil.rmtree(output)
                output.mkdir(parents=True, exist_ok=True)
                module = load_input_module(code_dir)
                payload, uses_storage, uses_nosql = prepare_input(name, size, module, services)
                staged = stage_function(name, code_dir, build_root)
                (staged / "job.json").write_text(
                    json.dumps({"benchmark": name, "input": payload, "repetitions": reps}) + "\n"
                )
                code = run_container(
                    staged,
                    output,
                    cpu,
                    timeout_for(name, reps, size),
                    services,
                    uses_storage,
                    uses_nosql,
                )
                chown_tree(output)
                status_path = output / "status.json"
                status = json.loads(status_path.read_text()) if status_path.is_file() else {}
                papi_ok = all(status.get(event) == 0 for event in PAPI_SETS)
                ok = code == 0 and status.get("uninstrumented") == 0 and papi_ok
                if not ok:
                    raise RuntimeError(f"exit {code} status {status}")
                package_bytes = directory_bytes(staged / "function") + closure_bytes(
                    catalog, DEP_SEEDS.get(name, [])
                )
                (output / "package.json").write_text(
                    json.dumps({"package_size_bytes": package_bytes}) + "\n"
                )
                succeeded.append(name)
                print(f"{name} recorded, package_size_bytes={package_bytes}", flush=True)
            except Exception as exc:
                print(f"{name} failed: {exc}", flush=True)
                if resume and output.exists():
                    (output / "failure.txt").write_text(str(exc) + "\n")
                    continue
                destination = failed_root / name
                if destination.exists():
                    shutil.rmtree(destination)
                destination.parent.mkdir(parents=True, exist_ok=True)
                if output.exists():
                    chown_tree(output)
                    shutil.move(str(output), str(destination))
                (destination / "failure.txt").write_text(str(exc) + "\n")
        write_manifest(results, cpu, repetitions, succeeded, size)
    finally:
        services.stop()
    return succeeded


def ordered(names: list[str]) -> list[str]:
    body = [name for name in names if name != "010.sleep"]
    if "010.sleep" in names:
        body.append("010.sleep")
    return body


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Collect local PAPI characterization measurements.")
    parser.add_argument("--results", type=Path, default=ROOT / "characterization" / "results" / "raw")
    parser.add_argument("--benchmark", action="append", default=None, help="Benchmark id. Repeatable.")
    parser.add_argument("--size", default="small", choices=["test", "small", "large"])
    parser.add_argument(
        "--repetitions",
        type=int,
        default=None,
        help="Handler repetitions for every benchmark. Default is 1 for long benchmarks and 3 otherwise.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Keep finished benchmarks and finished measurement passes.",
    )
    parser.add_argument("--rebuild-image", action="store_true")
    parser.add_argument("--probe-only", action="store_true")
    args = parser.parse_args(argv)

    require_docker()
    if args.rebuild_image or not image_present():
        build_image()
    cpu = pick_cpu()
    print(f"Pinned CPU {cpu}", flush=True)
    probe_papi(args.results, cpu)
    if args.probe_only:
        return
    selected = args.benchmark or list(SUITE)
    unknown = [name for name in selected if name not in SUITE]
    if unknown:
        raise SystemExit(f"Not in the characterization suite: {', '.join(unknown)}")
    args.results.mkdir(parents=True, exist_ok=True)
    if args.repetitions is not None and args.repetitions < 1:
        raise SystemExit("--repetitions must be at least 1")
    succeeded = collect(
        ordered(selected), args.results, args.size, cpu, args.repetitions, args.resume
    )
    on_disk = [
        name
        for name in SUITE
        if benchmark_complete(args.results / "local" / name)
    ]
    missing = [name for name in selected if name not in on_disk]
    print(f"Succeeded this run: {', '.join(succeeded) if succeeded else 'none'}", flush=True)
    print(f"Complete on disk: {', '.join(on_disk) if on_disk else 'none'}", flush=True)
    if missing:
        raise SystemExit(f"Missing measurements: {', '.join(missing)}")
    if len(on_disk) < 3:
        raise SystemExit("Fewer than 3 benchmarks produced measurements.")


if __name__ == "__main__":
    main()
