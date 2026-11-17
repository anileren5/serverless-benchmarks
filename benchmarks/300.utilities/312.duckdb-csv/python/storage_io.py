import json
from pathlib import Path, PurePosixPath


def relative_path(name):
    """Reject unsafe object paths."""
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or ".." in path.parts
            or "\\" in name or path.as_posix() != name or name == "."):
        raise ValueError(f"Invalid relative dataset path: {name}")
    return Path(*path.parts)


def download_dataset(client, bucket, prefix, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path = directory / "manifest.json"
    client.download(bucket, prefix.rstrip("/") + "/manifest.json", str(manifest_path))
    with manifest_path.open(encoding="utf-8") as source:
        manifest = json.load(source)
    paths = [manifest_path]
    for name in manifest["inputs"]:
        relative = relative_path(name)
        if name not in ("products.csv", "stores.csv") and not (
            len(relative.parts) == 2 and relative.parts[0] == "transactions" and relative.suffix == ".csv"
        ):
            raise ValueError(f"Unexpected input file: {name}")
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        client.download(bucket, prefix.rstrip("/") + "/" + name, str(path))
        paths.append(path)
    return manifest, {"download_files": len(paths), "download_size": sum(p.stat().st_size for p in paths)}
