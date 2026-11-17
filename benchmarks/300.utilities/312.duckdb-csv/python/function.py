import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter_ns
from uuid import uuid4

from . import storage
from .contract import DEFAULT_MEMORY_MB
from .etl_workload import transform
from .storage_io import download_dataset


def _resources(event):
    """Choose resource settings from Lambda or the event."""
    requested = event.get("resources", {})
    allocation = os.environ.get("AWS_LAMBDA_FUNCTION_MEMORY_SIZE")
    if allocation is not None:
        memory_mb = int(allocation)
        if "memory_mb" in requested and requested["memory_mb"] != memory_mb:
            raise ValueError("Requested memory_mb differs from Lambda's actual allocation")
    else:
        memory_mb = requested.get("memory_mb")
        if memory_mb is None:
            memory_mb = int(os.environ.get("SEBS_MEMORY_MB", str(DEFAULT_MEMORY_MB)))
    return {"memory_mb": memory_mb, "threads": requested.get("threads"),
            "memory_limit": requested.get("memory_limit")}


def _upload_output(client, bucket, prefix, directory):
    files, size = [], 0
    for path in sorted(directory.rglob("*.parquet")):
        name = path.relative_to(directory).as_posix()
        key = client.upload(bucket, prefix + "/" + name, str(path))
        files.append({"path": name, "key": key})
        size += path.stat().st_size
    return files, {"upload_files": len(files), "upload_size": size}


def handler(event):
    begin = perf_counter_ns()
    resources = _resources(event)
    profile = event.get("profile", False)
    bucket = event["bucket"]["bucket"]
    prefix = event["bucket"]["output"].rstrip("/") + "/" + uuid4().hex
    client = storage.storage.get_instance()
    with TemporaryDirectory(prefix="sebs-retail-", dir="/tmp") as temporary:
        work = Path(temporary)

        downloading = perf_counter_ns()
        manifest, transfers = download_dataset(client, bucket, event["object"]["prefix"], work / "input")
        downloaded = perf_counter_ns()

        if manifest["config"] != event["config"]:
            raise ValueError("Downloaded input configuration differs from invocation")
        phases = {}
        audit = transform(work / "input", work / "output", **resources,
                          measurements=phases, profile=profile)
        effective = phases.pop("resources")

        uploading = perf_counter_ns()
        files, uploaded_files = _upload_output(client, bucket, prefix, work / "output")
        uploaded = perf_counter_ns()

    total_time = (perf_counter_ns() - begin) / 1000

    phases.update(transfers)
    phases.update(uploaded_files)
    phases.update({"download_time": (downloaded - downloading) / 1000,
                   "upload_time": (uploaded - uploading) / 1000, "total_time": total_time})
    return {
        "result": {"bucket": bucket, "prefix": prefix,
                   "files": files, "audit": audit, "resources": effective},
        "measurement": phases,
    }
