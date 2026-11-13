import json
import os
import sys
import time
import uuid
from datetime import datetime
from multiprocessing.pool import ThreadPool
from typing import Any, List, Optional, Tuple, TYPE_CHECKING

import pandas as pd

from sebs.experiments.config import Config as ExperimentConfig
from sebs.experiments.experiment import Experiment
from sebs.experiments.recorded_run import RecordedRun
from sebs.faas.function import ExecutionResult, Trigger
from sebs.faas.system import System as FaaSSystem

# import cycle
if TYPE_CHECKING:
    from sebs import SeBS


class GangScheduling(Experiment, RecordedRun):
    """
    Launches N ranks of a bulk-synchronous benchmark (506.data-parallel-sgd) at the same time.

    The ranks exchange data at every iteration, so a run completes only if the platform runs
    all N at once. Platforms cap how many instances run concurrently (AWS: account concurrency;
    OpenWhisk: invoker memory / action memory), and above that cap the run deadlocks:
    running ranks wait at the barrier for ranks that are throttled or queued.

    Each run writes config.json, meta.json, raw.jsonl (one line per rank),
    and the process step adds summary.csv (one row per launch).
    """

    RESULTS_DIR = "gang-scheduling"

    def __init__(self, config: ExperimentConfig, is_workflow: bool = False):
        super().__init__(config)
        if is_workflow:
            raise RuntimeError(f"Experiment {self.name()} does not support workflows!")

    @staticmethod
    def name() -> str:
        return "gang-scheduling"

    @staticmethod
    def typename() -> str:
        return "Experiment.GangScheduling"

    def prepare(self, sebs_client: "SeBS", deployment_client: FaaSSystem):
        self._settings = self.config.experiment_settings(self.name())
        self._deployment_client = deployment_client
        self._is_local = deployment_client.name() == "local"
        if self._is_local:
            raise RuntimeError("The local deployment has a single function container")
        self._benchmark = sebs_client.get_benchmark(
            self._settings["benchmark"], deployment_client, self.config
        )
        self._input = self._benchmark.prepare_input(
            deployment_client.system_resources,
            size=self._settings["input-size"],
            replace_existing=self.config.update_storage,
        )
        self._function = deployment_client.get_function(self._benchmark)
        self._function.config.memory = self._settings["memory"]
        self._function.config.timeout = self._settings.get("timeout", self._function.config.timeout)
        deployment_client.update_function_configuration(self._function, self._benchmark)
        deployment_client.cache_client.update_benchmark(self._function)
        triggers = self._function.triggers(Trigger.TriggerType.LIBRARY)
        self._trigger = (
            triggers[0]
            if triggers
            else deployment_client.create_trigger(self._function, Trigger.TriggerType.LIBRARY)
        )

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
        self._write_json("meta.json", meta)

        payload = {**self._input, **self._settings.get("input", {})}
        with open(os.path.join(self._run_dir, "raw.jsonl"), "w") as raw:
            for world_size in self._settings["world-sizes"]:
                for repetition in range(self._settings["repetitions"]):
                    self._launch(world_size, repetition, payload, raw)
                    time.sleep(self._settings.get("pause-seconds", 5))

        meta["end"] = datetime.now().isoformat()
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
                self.process_run(run_dir)

    def _launch(self, world_size: int, repetition: int, payload: dict, raw):
        run_id = uuid.uuid4().hex[:12]
        payloads = [
            {**payload, "rank": rank, "world_size": world_size, "run_id": run_id}
            for rank in range(world_size)
        ]
        launch = time.time()
        with ThreadPool(world_size) as pool:
            results = pool.map(self._invoke, payloads)
        failures = 0
        for rank, (client_begin, result, error) in enumerate(results):
            record = {
                "world_size": world_size,
                "repetition": repetition,
                "run_id": run_id,
                "rank": rank,
                "launch": launch,
                "client_begin": client_begin,
                **self._record(result, error),
            }
            failures += record["status"] != "completed"
            raw.write(json.dumps(record) + "\n")
        raw.flush()
        self.logging.info(
            f"N={world_size} repetition {repetition}: {world_size - failures} of "
            f"{world_size} ranks completed, {time.time() - launch:.1f} s"
        )

    def _invoke(self, payload: dict) -> Tuple[float, Optional[ExecutionResult], Optional[str]]:
        begin = time.time()
        try:
            return begin, self._trigger.sync_invoke(payload), None
        except Exception as e:
            return begin, None, str(e)

    @staticmethod
    def _record(result: Optional[ExecutionResult], error: Optional[str]) -> dict:
        if result is None:
            return {"status": "invocation_error", "error": error}
        record: dict = {
            "request_id": result.request_id,
            "is_cold": result.stats.cold_start,
            "client_time_us": result.times.client,
            "billed_time_ms": result.billing.billed_time,
        }
        if result.stats.failure:
            output: Any = result.output
            record.update({"status": "function_error", "error": str(output.get("error"))[:500]})
            return record
        rank = rank_result(result.output)
        record.update({"status": rank.get("status", "unknown"), "rank_result": rank})
        return record

    @staticmethod
    def process_run(run_dir: str):
        with open(os.path.join(run_dir, "raw.jsonl")) as raw:
            records = [json.loads(line) for line in raw]
        summarize(records).to_csv(os.path.join(run_dir, "summary.csv"), index=False)


def rank_result(output: dict) -> dict:
    """The wrappers nest the benchmark result differently on each platform."""
    node: Any = output
    while isinstance(node, dict) and "run_id" not in node:
        node = node.get("output", node.get("result"))
    return node if isinstance(node, dict) else {}


def summarize(records: List[dict]) -> pd.DataFrame:
    """One row per launch: how many ranks completed, start skew, and time spent at barriers."""
    rows = []
    frame = pd.DataFrame(records)
    for (world_size, repetition), launch in frame.groupby(["world_size", "repetition"]):
        ranks = [r for r in launch.get("rank_result", []) if isinstance(r, dict) and r]
        begins = [r["begin"] for r in ranks]
        waits = [sum(i["wait_seconds"] for i in r["iterations"]) for r in ranks]
        durations = [r["end"] - r["begin"] for r in ranks]
        completed = int((launch["status"] == "completed").sum())
        rows.append(
            {
                "world_size": world_size,
                "repetition": repetition,
                "completed_ranks": completed,
                "run_completed": completed == world_size,
                "barrier_timeouts": int((launch["status"] == "barrier_timeout").sum()),
                "invocation_errors": int((launch["status"] == "invocation_error").sum()),
                "function_errors": int((launch["status"] == "function_error").sum()),
                "ranks_started": len(ranks),
                "start_skew_seconds": max(begins) - min(begins) if begins else None,
                "first_rank_start_seconds": min(begins) - launch["launch"].iloc[0]
                if begins
                else None,
                "makespan_seconds": max(r["end"] for r in ranks) - launch["launch"].iloc[0]
                if ranks
                else None,
                "wait_fraction_median": pd.Series(waits).div(durations).median() if ranks else None,
                "first_barrier_wait_max": max(r["iterations"][0]["wait_seconds"] for r in ranks)
                if ranks and all(r["iterations"] for r in ranks)
                else None,
                "weights_consistent": len({r["weights_checksum"] for r in ranks}) == 1
                if completed == world_size
                else None,
                "errors": "; ".join(sorted({str(e)[:120] for e in launch["error"] if pd.notna(e)}))
                if "error" in launch
                else "",
            }
        )
    return pd.DataFrame(rows)


if __name__ == "__main__":
    for directory in sys.argv[1:]:
        GangScheduling.process_run(directory)
