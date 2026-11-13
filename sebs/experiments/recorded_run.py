import json
import os
import platform
import subprocess
import sys
from typing import Any, Optional

from sebs.version import __version__


class RecordedRun:
    """Provenance of a run directory: config, git state, client, and Docker setup.

    Expects the experiment to set _benchmark, _function, _is_local, and _run_dir.
    """

    _benchmark: Any
    _function: Any
    _is_local: bool
    _run_dir: str

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
