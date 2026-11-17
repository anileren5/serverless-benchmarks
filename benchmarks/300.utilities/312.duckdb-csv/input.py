from dataclasses import asdict, replace
import hashlib
from importlib import import_module
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
from types import ModuleType

_directory = Path(__file__).resolve().parent
_package_name = "_sebs_duckdb_csv_" + hashlib.sha256(str(_directory).encode()).hexdigest()
if _package_name not in sys.modules:
    _package = ModuleType(_package_name)
    _package.__path__ = [str(_directory)]
    sys.modules[_package_name] = _package
validator = import_module(_package_name + ".validation")
generator = import_module(_package_name + ".python.generator")
transfers = import_module(_package_name + ".python.storage_io")


def buckets_count():
    return (1, 1)


def _profiles(data_dir):
    """Load profile overrides."""
    path = Path(data_dir) / "retail-config.json" if data_dir else None
    if path is None or not path.exists():
        return generator.PROFILES
    with path.open(encoding="utf-8") as source:
        overrides = json.load(source)
    if set(overrides) - generator.PROFILES.keys():
        raise ValueError("retail-config.json contains unknown profile names")
    allowed = {"file_count"}
    for changes in overrides.values():
        if set(changes) - allowed:
            raise ValueError("retail-config.json contains unsupported experimental controls")
    return {size: replace(config, **overrides.get(size, {})) for size, config in generator.PROFILES.items()}


def _dataset_prefix(config):
    encoded = json.dumps(asdict(config), sort_keys=True, separators=(",", ":")).encode()
    return f"{config.size}/{hashlib.sha256(encoded).hexdigest()}"


def _runtime_settings(data_dir):
    """Read invocation controls independently of dataset generation settings."""
    settings = {}
    if data_dir is not None:
        path = Path(data_dir) / "retail-runtime.json"
        try:
            with path.open(encoding="utf-8") as source:
                settings = json.load(source)
        except FileNotFoundError:
            pass
    if not isinstance(settings, dict) or set(settings) - {"resources", "profile"}:
        raise ValueError("retail-runtime.json must be an object with only resources and profile")
    resources = settings.get("resources", {})
    if not isinstance(resources, dict) or set(resources) - {"threads", "memory_limit"}:
        raise ValueError("retail-runtime.json resources must contain only threads and memory_limit")
    if "threads" in resources and (type(resources["threads"]) is not int or resources["threads"] < 1):
        raise ValueError("retail-runtime.json resources.threads must be a positive integer")
    if "memory_limit" in resources and (
        not isinstance(resources["memory_limit"], str) or not resources["memory_limit"].strip()
    ):
        raise ValueError("retail-runtime.json resources.memory_limit must be a nonempty DuckDB memory-size string")
    profile = settings.get("profile", False)
    if type(profile) is not bool:
        raise ValueError("retail-runtime.json profile must be a boolean")
    return {"resources": resources, "profile": profile}


def generate_input(data_dir, size, benchmarks_bucket, input_paths, output_paths, upload_func, nosql_func):
    runtime = _runtime_settings(data_dir)
    profiles = _profiles(data_dir)
    if size not in profiles:
        raise ValueError(f"Unknown retail profile: {size}")
    with TemporaryDirectory(prefix="sebs-retail-input-") as temporary:
        for name, config in profiles.items():
            directory = Path(temporary) / name
            manifest = generator.generate_dataset(directory, config)
            validator.validate_inputs(directory)
            prefix = _dataset_prefix(config)
            for filename in [*manifest["inputs"], "manifest.json"]:
                upload_func(0, prefix + "/" + filename, str(directory / filename))
    config = profiles[size]
    return {
        **runtime,
        "size": size, "config": asdict(config),
        "bucket": {"bucket": benchmarks_bucket, "input": input_paths[0], "output": output_paths[0]},
        "object": {"prefix": input_paths[0].rstrip("/") + "/" + _dataset_prefix(config)},
    }


def _download_outputs(storage, result, directory):
    directory.mkdir()
    keys, paths = set(), set()
    for entry in result["files"]:
        relative = transfers.relative_path(entry["path"])
        key = entry["key"]
        if (relative.suffix != ".parquet" or entry["path"] in paths or key in keys
                or not key.startswith(result["prefix"] + "/")):
            raise ValueError("Duplicate or misplaced output object")
        paths.add(entry["path"])
        keys.add(key)
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        storage.download(result["bucket"], key, str(path))


def validate_output(data_dir, input_config, output, language, storage=None):
    try:
        result = output["result"]
        if result["bucket"] != input_config["bucket"]["bucket"]:
            raise ValueError("Unexpected result bucket")
        transfers.relative_path(result["prefix"])
        prefix = input_config["bucket"]["output"].rstrip("/") + "/"
        if not result["prefix"].startswith(prefix) or result["prefix"] == prefix:
            raise ValueError("Output prefix is outside the invocation output area")
        if storage is None:
            return "Retail logical-output validation requires a storage client"
        with TemporaryDirectory(prefix="sebs-retail-validation-") as temporary:
            work = Path(temporary)
            manifest, _ = transfers.download_dataset(
                storage, result["bucket"], input_config["object"]["prefix"], work / "input")
            if manifest["config"] != input_config["config"]:
                raise ValueError("Input manifest configuration differs from invocation")
            directory = work / "output"
            _download_outputs(storage, result, directory)
            checked = validator.validate_output(directory, work / "input")
            if (result["audit"] != checked["audit"]
                    or any(type(value) is not int for value in result["audit"].values())):
                raise ValueError("Incorrect audit summary")
        return None
    except Exception as error:
        return f"Retail ETL validation failed: {error}"
