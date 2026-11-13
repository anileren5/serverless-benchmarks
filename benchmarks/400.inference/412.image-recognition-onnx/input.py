size_generators = {
    "test": {"inferences": 1, "threads": [1, 2], "idle_seconds": 0.1},
    "small": {"inferences": 4, "idle_seconds": 0.2},
    "large": {"inferences": 16, "idle_seconds": 0.2},
}


def buckets_count():
    return (0, 0)


def generate_input(
    data_dir, size, benchmarks_bucket, input_paths, output_paths, upload_func, nosql_func
):
    return dict(size_generators[size])


def validate_output(data_dir, input_config, output, language, storage=None):
    runs = output.get("result", {}).get("runs", [])
    if not runs:
        return "No inference runs in output"
    classes = {run["top1"] for run in runs}
    if len(classes) != 1:
        return f"Top-1 class differs between thread counts: {sorted(classes)}"
    return None
