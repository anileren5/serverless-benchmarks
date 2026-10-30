size_generators = {
    "test": {"grid_size": 128, "iterations": 100, "spin_seconds": 0.2, "idle_seconds": 0.1},
    "small": {"grid_size": 1024, "iterations": 200, "spin_seconds": 0.5, "idle_seconds": 0.2},
    "large": {"grid_size": 2048, "iterations": 500, "spin_seconds": 1.0, "idle_seconds": 0.2},
}


def buckets_count():
    return (0, 0)


def generate_input(
    data_dir, size, benchmarks_bucket, input_paths, output_paths, upload_func, nosql_func
):
    return dict(size_generators[size])


def validate_output(data_dir, input_config, output, language, storage=None):
    runs = output.get("result", {}).get("jacobi", [])
    if not runs:
        return "No Jacobi runs in output"
    checksums = {run["checksum"] for run in runs}
    if len(checksums) != 1:
        return f"Jacobi checksum differs between thread counts: {sorted(checksums)}"
    return None
