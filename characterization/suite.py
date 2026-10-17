"""Benchmarks included in the characterization suite.

Python implements every application benchmark, so the reported PCA uses Python.
``010.sleep`` is the low-compute control. Internal helpers used by other SeBS
experiments are listed separately and stay out of the suite PCA.
"""

from __future__ import annotations

# Order matches the numeric SeBS layout and the table in ROADMAP.md.
SUITE = (
    "010.sleep",
    "110.dynamic-html",
    "120.uploader",
    "130.crud-api",
    "210.thumbnailer",
    "220.video-processing",
    "311.compression",
    "411.image-recognition",
    "501.graph-pagerank",
    "502.graph-mst",
    "503.graph-bfs",
    "504.dna-visualisation",
)

# Invoked by other experiments. Useful as a platform probe, not as a suite row.
EXCLUDED_MICROBENCHMARKS = (
    "020.network-benchmark",
    "030.clock-synchronization",
    "040.server-reply",
)

# Iso-memory setting. Lambda ties vCPU share to memory, and 504 requires 2048 MB.
CHARACTERIZATION_MEMORY_MB = 2048
CHARACTERIZATION_INPUT_SIZE = "small"
CHARACTERIZATION_REPETITIONS = 30
CHARACTERIZATION_LANGUAGE = "python"
CHARACTERIZATION_LANGUAGE_VERSION = "3.11"


def main() -> None:
    """Print one suite benchmark per line for the collection script."""
    for name in SUITE:
        print(name)


if __name__ == "__main__":
    main()
