size_generators = {
    "test": {"iterations": 3, "samples_per_rank": 2000, "features": 64},
    "small": {"iterations": 20, "samples_per_rank": 20000, "features": 256},
    "large": {"iterations": 50, "samples_per_rank": 50000, "features": 512},
}


def buckets_count():
    return (0, 1)


def generate_input(
    data_dir, size, benchmarks_bucket, input_paths, output_paths, upload_func, nosql_func
):
    """Single-rank input; the gang-scheduling experiment adds rank, world_size, and run_id."""
    return {
        **size_generators[size],
        "rank": 0,
        "world_size": 1,
        "run_id": "single",
        "learning_rate": 0.5,
        "barrier_timeout": 30.0,
        "poll_interval": 0.05,
        "bucket": {"bucket": benchmarks_bucket, "output": output_paths[0]},
    }


def validate_output(data_dir, input_config, output, language, storage=None):
    result = output.get("result", {})
    if result.get("status") != "completed":
        return f"Rank did not complete: {result.get('status')}"
    if not result.get("loss") or result["loss"][-1] >= result["loss"][0]:
        return "Training loss did not decrease"
    return None
