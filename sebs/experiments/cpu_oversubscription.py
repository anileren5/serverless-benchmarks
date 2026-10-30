import json
import os
import platform
import subprocess
import sys
import time
from datetime import datetime
from multiprocessing.pool import ThreadPool
from typing import Any, cast, Iterator, List, Optional, Tuple, TYPE_CHECKING

import pandas as pd
import requests

from sebs.experiments.config import Config as ExperimentConfig
from sebs.experiments.experiment import Experiment
from sebs.faas.function import ExecutionResult, Trigger
from sebs.faas.system import System as FaaSSystem
from sebs.version import __version__

# import cycle
if TYPE_CHECKING:
    from sebs import SeBS
    from sebs.local.function import LocalFunction


class CPUOversubscription(Experiment):
    """
    Sweeps the CPU resources granted to 505.cpu-oversubscription and records every invocation.

    Cloud platforms: the memory size of the function is swept, since it determines the CPU share.
    Local deployment: Docker CPU limits (CFS quota, cpuset, shares with contention) are applied
    to the function container; this is the ground truth for interpreting cloud measurements.

    Each run writes a self-contained directory: config.json, meta.json, raw.jsonl.
    The process step adds processed.csv (one row per kernel run) and summary.csv.
    """

    RESULTS_DIR = "cpu-oversubscription"
    DOCKER_CPU_PERIOD = 100000
    DEFAULT_CPU_SHARES = 1024

    def __init__(self, config: ExperimentConfig, is_workflow: bool = False):
        super().__init__(config)
        if is_workflow:
            raise RuntimeError(f"Experiment {self.name()} does not support workflows!")
        self._competitors: list = []

    @staticmethod
    def name() -> str:
        return "cpu-oversubscription"

    @staticmethod
    def typename() -> str:
        return "Experiment.CPUOversubscription"

    def prepare(self, sebs_client: "SeBS", deployment_client: FaaSSystem):
        self._settings = self.config.experiment_settings(self.name())
        self._deployment_client = deployment_client
        self._is_local = deployment_client.name() == "local"
        self._benchmark = sebs_client.get_benchmark(
            self._settings["benchmark"], deployment_client, self.config
        )
        # Deployments can only configure a function after its input has been processed.
        self._inputs = {
            size: self._benchmark.prepare_input(
                deployment_client.system_resources,
                size=size,
                replace_existing=self.config.update_storage,
            )
            for size in {c["input-size"] for c in self._settings["configurations"]}
        }
        self._function = deployment_client.get_function(self._benchmark)
        self._trigger = self._get_trigger()

        name = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}_{deployment_client.name()}"
        if "label" in self._settings:
            name += f"_{self._settings['label']}"
        self._run_dir = os.path.join(sebs_client.output_dir, self.RESULTS_DIR, name)
        os.makedirs(self._run_dir)
        self.logging.info(f"Results of this run will be stored in {self._run_dir}")

    def run(self):
        begin = datetime.now()
        self._write_json(
            "config.json",
            {
                "experiment": self._settings,
                "deployment": {
                    "name": self._deployment_client.name(),
                    "region": self._deployment_client.config.region,
                },
                "runtime": self.config.runtime.serialize(),
                "architecture": self.config.architecture,
            },
        )
        meta = self._metadata()
        meta["begin"] = begin.isoformat()
        meta["client"]["load_average_begin"] = os.getloadavg()
        self._write_json("meta.json", meta)

        try:
            with open(os.path.join(self._run_dir, "raw.jsonl"), "w") as raw:
                for configuration in self._settings["configurations"]:
                    self._run_configuration(configuration, raw)
        finally:
            self._stop_competitors()
            if self._is_local:
                self._apply_local_limit({"name": "unlimited"})
            meta["end"] = datetime.now().isoformat()
            meta["client"]["load_average_end"] = os.getloadavg()
            self._write_json("meta.json", meta)

        self.process_run(self._run_dir)
        self.logging.info(f"Finished, results stored in {self._run_dir}")

    def process(
        self,
        sebs_client: "SeBS",
        deployment_client: FaaSSystem,
        directory: str,
        logging_filename: str,
        extend_time_interval: int,
    ):
        results_dir = os.path.join(directory, self.RESULTS_DIR)
        for run in sorted(os.listdir(results_dir)):
            run_dir = os.path.join(results_dir, run)
            if os.path.exists(os.path.join(run_dir, "raw.jsonl")):
                self.logging.info(f"Processing {run_dir}")
                self.process_run(run_dir)

    def _run_configuration(self, configuration: dict, raw):
        name = configuration["name"]
        payload = {**self._inputs[configuration["input-size"]], **configuration.get("input", {})}

        for resources, label in self._resource_settings(configuration):
            for policy in configuration.get("omp-wait-policies", [None]):
                self.logging.info(f"Configuration {name}: resources {label}, policy {policy}")
                cell = {
                    "configuration": name,
                    "platform": self._deployment_client.name(),
                    "resources": label,
                    "memory": resources.get("memory"),
                    "cpu_limit": resources.get("cpu-limit"),
                    "input_size": configuration["input-size"],
                    "omp_wait_policy_requested": policy,
                    "cold_starts": configuration.get("cold-starts", False),
                }
                cell_payload = dict(payload)
                if policy is not None:
                    cell_payload["omp_wait_policy"] = policy
                self._measure_cell(configuration, cell, cell_payload, raw)
            self._stop_competitors()

    def _resource_settings(self, configuration: dict) -> Iterator[Tuple[dict, str]]:
        """Applies each resource setting of the configuration before yielding it."""
        if self._is_local:
            if "memory-sizes" in configuration:
                raise RuntimeError("Local deployment does not support memory-sizes, use cpu-limits")
            for limit in configuration.get("cpu-limits", [{"name": "unlimited"}]):
                self._apply_local_limit(limit)
                yield {"cpu-limit": limit}, limit["name"]
        else:
            if "cpu-limits" in configuration:
                raise RuntimeError("cpu-limits are only supported by the local deployment")
            for memory in configuration["memory-sizes"]:
                self._function.config.memory = memory
                self._deployment_client.update_function_configuration(
                    self._function, self._benchmark
                )
                self._deployment_client.cache_client.update_benchmark(self._function)
                yield {"memory": memory}, f"{memory}MB"

    def _measure_cell(self, configuration: dict, cell: dict, payload: dict, raw):
        repetitions = configuration["repetitions"]
        cold_starts = configuration.get("cold-starts", False)
        concurrency = configuration.get("concurrent-invocations", 1)
        if self._is_local and concurrency > 1:
            self.logging.warning("Local deployment has a single container, using 1 invocation")
            concurrency = 1

        # The OpenMP wait policy is fixed once the kernel library is loaded,
        # hence every cell starts on fresh sandboxes.
        self._fresh_sandboxes()
        if not cold_starts:
            self._invoke_batch(cell, payload, concurrency, "warmup", 0, raw)

        batch = 0
        invoked = 0
        while invoked < repetitions:
            if cold_starts and batch > 0:
                self._fresh_sandboxes()
            count = min(concurrency, repetitions - invoked)
            self._invoke_batch(cell, payload, count, "measurement", batch, raw)
            invoked += count
            batch += 1

    def _invoke_batch(self, cell: dict, payload: dict, count: int, phase: str, batch: int, raw):
        with ThreadPool(count) as pool:
            results = pool.map(self._invoke, [payload] * count)
        failures = 0
        for invocation_begin, result, error in results:
            record = {
                **cell,
                "phase": phase,
                "batch": batch,
                "invocation_begin": invocation_begin,
                **self._record(result, error),
            }
            failures += record["failure"]
            raw.write(json.dumps(record) + "\n")
        raw.flush()
        self.logging.info(f"{phase} batch {batch}: {count} invocations, {failures} failed")

    def _invoke(self, payload: dict) -> Tuple[str, Optional[ExecutionResult], Optional[str]]:
        begin = datetime.now().isoformat()
        try:
            return begin, self._trigger.sync_invoke(payload), None
        except Exception as e:
            return begin, None, str(e)

    def _record(self, result: Optional[ExecutionResult], error: Optional[str]) -> dict:
        if result is None:
            return {"failure": True, "error": error}
        record = {
            "failure": result.stats.failure,
            "error": None,
            "request_id": result.request_id,
            "is_cold": result.stats.cold_start,
            "container_id": result.output.get("container_id"),
            "client_time_us": result.times.client,
            "provider_init_us": result.provider_times.initialization,
            "billed_time_ms": result.billing.billed_time,
            "memory_used": result.stats.memory_used,
        }
        if self._is_local:
            record["container_id"] = self._local_function.container.id[:12]
        if result.stats.failure:
            record["error"] = result.output.get("error", "Invocation failed")
        else:
            record["benchmark"] = benchmark_result(result.output)
        return record

    @property
    def _local_function(self) -> "LocalFunction":
        return cast("LocalFunction", self._function)

    def _get_trigger(self) -> Trigger:
        trigger_type = Trigger.TriggerType.HTTP if self._is_local else Trigger.TriggerType.LIBRARY
        triggers = self._function.triggers(trigger_type)
        if triggers:
            return triggers[0]
        return self._deployment_client.create_trigger(self._function, trigger_type)

    def _fresh_sandboxes(self):
        if self._is_local:
            self._restart_local_container()
        else:
            try:
                self._deployment_client.enforce_cold_start([self._function], self._benchmark)
            except NotImplementedError:
                raise RuntimeError(
                    f"{self._deployment_client.name()} cannot enforce cold starts, "
                    f"which {self.name()} needs to switch sandboxes"
                )

    def _restart_local_container(self):
        from sebs.local.function import HTTPTrigger

        function = self._local_function
        function.restart()
        if function.url != cast(HTTPTrigger, self._trigger).url:
            self._trigger = HTTPTrigger(function.url)
        for _ in range(40):
            try:
                requests.get(f"http://{function.url}/alive", timeout=1)
                return
            except requests.exceptions.RequestException:
                time.sleep(0.25)
        raise RuntimeError(f"Function container {function.container.id} did not restart")

    def _apply_local_limit(self, limit: dict):
        import docker

        client = docker.from_env()
        host_cpus = client.info()["NCPU"]
        container = self._local_function.container
        container.update(
            cpu_period=self.DOCKER_CPU_PERIOD,
            cpu_quota=int(limit["cpus"] * self.DOCKER_CPU_PERIOD) if "cpus" in limit else -1,
            cpuset_cpus=limit.get("cpuset", f"0-{host_cpus - 1}"),
            cpu_shares=limit.get("cpu-shares", self.DEFAULT_CPU_SHARES),
        )

        competitor = limit.get("competitor")
        if competitor is not None:
            threads = competitor.get("threads", host_cpus)
            busy_loops = f"for i in $(seq {threads}); do while :; do :; done & done; wait"
            self._competitors.append(
                client.containers.run(
                    container.image,
                    entrypoint=["/bin/bash", "-c", busy_loops],
                    cpu_shares=competitor.get("cpu-shares", self.DEFAULT_CPU_SHARES),
                    cpuset_cpus=competitor.get("cpuset", f"0-{host_cpus - 1}"),
                    detach=True,
                    remove=True,
                )
            )
            self.logging.info(f"Started competitor container with {threads} busy threads")

    def _stop_competitors(self):
        for competitor in self._competitors:
            competitor.stop(timeout=0)
        self._competitors = []

    def _metadata(self) -> dict:
        repository = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

        def git(*args: str) -> Optional[str]:
            try:
                return subprocess.run(
                    ["git", *args], cwd=repository, capture_output=True, text=True, check=True
                ).stdout.strip()
            except (OSError, subprocess.CalledProcessError):
                return None

        status = git("status", "--porcelain")
        meta = {
            "sebs_version": __version__,
            "git_commit": git("rev-parse", "HEAD"),
            "git_branch": git("rev-parse", "--abbrev-ref", "HEAD"),
            "git_dirty_files": status.splitlines() if status else [],
            "benchmark_hash": self._benchmark.hash,
            "function_name": self._function.name,
            "command": sys.argv,
            "client": {
                "hostname": platform.node(),
                "kernel": platform.release(),
                "cpus": os.cpu_count(),
                "python": platform.python_version(),
            },
        }
        if self._is_local:
            import docker

            info = docker.from_env().info()
            meta["docker"] = {
                key: info.get(key)
                for key in ("ServerVersion", "CgroupVersion", "CgroupDriver", "NCPU", "MemTotal")
            }
        return meta

    def _write_json(self, name: str, content: dict):
        with open(os.path.join(self._run_dir, name), "w") as out:
            json.dump(content, out, indent=2)

    @staticmethod
    def process_run(run_dir: str):
        with open(os.path.join(run_dir, "raw.jsonl")) as raw:
            rows = [row for line in raw for row in flatten(json.loads(line))]
        processed = pd.DataFrame(rows)
        processed.to_csv(os.path.join(run_dir, "processed.csv"), index=False)
        summarize(processed).to_csv(os.path.join(run_dir, "summary.csv"), index=False)


def benchmark_result(output: dict) -> dict:
    """The wrappers nest the benchmark result differently on each platform."""
    node: Any = output
    while isinstance(node, dict) and "environment" not in node:
        node = node.get("output", node.get("result"))
    return node if isinstance(node, dict) else {}


def flatten(record: dict) -> List[dict]:
    """One row per Jacobi run; failed invocations become a single row without kernel data."""
    invocation = {
        key: record.get(key)
        for key in (
            "configuration",
            "platform",
            "resources",
            "memory",
            "input_size",
            "omp_wait_policy_requested",
            "cold_starts",
            "phase",
            "batch",
            "invocation_begin",
            "failure",
            "error",
            "request_id",
            "is_cold",
            "container_id",
            "client_time_us",
            "provider_init_us",
            "billed_time_ms",
            "memory_used",
        )
    }

    benchmark = record.get("benchmark")
    if not benchmark:
        return [invocation]
    env = benchmark["environment"]
    cgroup = env.get("cgroup", {})
    effective = benchmark["effective_cpus"]
    invocation.update(
        {
            "omp_wait_policy_effective": env.get("omp_wait_policy"),
            "os_cpu_count": env.get("os_cpu_count"),
            "sched_affinity": env.get("sched_affinity"),
            "omp_num_procs": env.get("omp_num_procs"),
            "cpu_model": env.get("cpu_model"),
            "cpu_max": cgroup.get("/sys/fs/cgroup/cpu.max"),
            "cpu_weight": cgroup.get("/sys/fs/cgroup/cpu.weight"),
            "cpuset_effective": cgroup.get("/sys/fs/cgroup/cpuset.cpus.effective"),
            "effective_cpus": effective["effective_cpus"],
        }
    )
    rows = []
    for run in benchmark["jacobi"]:
        throttling = run.get("cgroup_cpu_stat_delta") or {}
        periods = throttling.get("nr_periods")
        throttled = throttling.get("nr_throttled")
        rows.append(
            {
                **invocation,
                "threads": run["threads"],
                "position": run.get("position"),
                "wall_seconds": run["wall_seconds"],
                "cpu_seconds": run["cpu_seconds"],
                "cpu_utilization": run["cpu_seconds"] / run["wall_seconds"],
                "checksum": run["checksum"],
                "nr_periods": periods,
                "nr_throttled": throttled,
                "throttled_fraction": throttled / periods if periods else None,
                "throttled_usec": throttling.get("throttled_usec"),
                "idle_cpu_seconds_after": run.get("idle_cpu_seconds_after"),
            }
        )
    return rows


def summarize(processed: pd.DataFrame) -> pd.DataFrame:
    """Median kernel time per cell and thread count, with speedup and penalty."""
    if "threads" not in processed:
        return pd.DataFrame()
    keys = ["configuration", "resources", "omp_wait_policy_requested", "cold_starts"]
    measured = processed[(processed["phase"] == "measurement") & processed["threads"].notna()]
    grouped = measured.groupby(keys + ["threads"], dropna=False)
    summary = grouped.agg(
        samples=("wall_seconds", "size"),
        wall_median=("wall_seconds", "median"),
        wall_p25=("wall_seconds", lambda x: x.quantile(0.25)),
        wall_p75=("wall_seconds", lambda x: x.quantile(0.75)),
        cpu_utilization_median=("cpu_utilization", "median"),
        throttled_fraction_median=("throttled_fraction", "median"),
        effective_cpus_median=("effective_cpus", "median"),
        os_cpu_count=("os_cpu_count", "max"),
        cold_fraction=("is_cold", "mean"),
    ).reset_index()

    def per_cell(cell: pd.DataFrame) -> pd.DataFrame:
        cell = cell.copy()
        single = cell.loc[cell["threads"] == 1, "wall_median"]
        cell["speedup"] = single.iloc[0] / cell["wall_median"] if len(single) else float("nan")
        cell["penalty_vs_best"] = cell["wall_median"] / cell["wall_median"].min()
        return cell

    cells = [per_cell(cell) for _, cell in summary.groupby(keys, dropna=False)]
    return pd.concat(cells, ignore_index=True) if cells else summary


if __name__ == "__main__":
    for directory in sys.argv[1:]:
        CPUOversubscription.process_run(directory)
