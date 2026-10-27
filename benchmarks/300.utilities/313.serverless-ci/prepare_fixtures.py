#!/usr/bin/env python3
# Copyright 2020-2026 ETH Zurich and the SeBS authors. All rights reserved.
"""Reproducible offline fixture builder for 313.serverless-ci.

Prepares pinned source and offline dependency archives for:
1. ts-pattern (v5.0.5, MIT License)
2. NestJS (v10.3.3, MIT License)

All archives and expected verification metadata (SHA-256 checksums, test counts,
and build output digests) are written to fixtures_manifest.json inside the
benchmark's benchmarks-data directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import tarfile
import tempfile
from typing import Any, Dict


TS_PATTERN_REPO = "https://github.com/gvergnaud/ts-pattern.git"
TS_PATTERN_TAG = "v5.0.5"

NESTJS_REPO = "https://github.com/nestjs/nest.git"
NESTJS_TAG = "v10.3.3"

KEEP_NEST_DEV_PREFIXES = (
    "@types/",
    "@fastify/",
    "@grpc/",
    "typescript",
    "ts-node",
    "tsconfig-paths",
    "mocha",
    "chai",
    "sinon",
    "supertest",
    "ws",
    "amqp",
    "mqtt",
    "nats",
    "redis",
    "ioredis",
    "kafkajs",
    "rxjs",
    "body-parser",
    "bytes",
    "cache-manager",
    "core-js",
    "engine.io-client",
    "eventsource",
    "fastify",
    "http-errors",
    "light-my-request",
    "multer",
    "socket.io-client",
)


def sha256_file(path: pathlib.Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def create_deterministic_tar_gz(source_dir: pathlib.Path, archive_path: pathlib.Path) -> None:
    """Create a deterministic .tar.gz archive from the contents of source_dir."""
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "w:gz", compresslevel=6) as tar:
        for root, dirs, files in os.walk(source_dir):
            dirs.sort()
            files.sort()
            rel_root = os.path.relpath(root, source_dir)
            for name in files:
                full_path = os.path.join(root, name)
                arcname = name if rel_root == "." else os.path.join(rel_root, name)
                tar.add(full_path, arcname=arcname, recursive=False)
            for dname in dirs:
                full_dpath = os.path.join(root, dname)
                if os.path.islink(full_dpath):
                    arcname = dname if rel_root == "." else os.path.join(rel_root, dname)
                    tar.add(full_dpath, arcname=arcname, recursive=False)


def prepare_ts_pattern(work_dir: pathlib.Path, out_dir: pathlib.Path) -> Dict[str, Any]:
    """Prepare source and dependency archives for ts-pattern v5.0.5."""
    repo_dir = work_dir / "ts-pattern"
    if not repo_dir.exists():
        subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", TS_PATTERN_TAG, TS_PATTERN_REPO, str(repo_dir)],
            check=True,
        )

    commit_sha = (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_dir).decode().strip()
    )
    shutil.rmtree(repo_dir / ".git", ignore_errors=True)
    shutil.rmtree(repo_dir / "node_modules", ignore_errors=True)
    shutil.rmtree(repo_dir / "dist", ignore_errors=True)

    # Fix macOS-only BSD `sed -i ''` in scripts/generate-cts.sh so Linux builds succeed
    gen_cts = repo_dir / "scripts" / "generate-cts.sh"
    if gen_cts.exists():
        content = gen_cts.read_text()
        content = content.replace("sed -i '' -e", "sed -i -e")
        gen_cts.write_text(content)

    source_archive = out_dir / "ts-pattern-v5.0.5-source.tar.gz"
    create_deterministic_tar_gz(repo_dir, source_archive)

    subprocess.run(["npm", "ci", "--ignore-scripts"], cwd=repo_dir, check=True)
    deps_archive = out_dir / "ts-pattern-v5.0.5-deps.tar.gz"
    create_deterministic_tar_gz(repo_dir / "node_modules", deps_archive)

    return {
        "project": "ts-pattern",
        "version": TS_PATTERN_TAG,
        "commit": commit_sha,
        "license": "MIT",
        "source_archive": source_archive.name,
        "source_sha256": sha256_file(source_archive),
        "source_size_bytes": source_archive.stat().st_size,
        "deps_archive": deps_archive.name,
        "deps_sha256": sha256_file(deps_archive),
        "deps_size_bytes": deps_archive.stat().st_size,
    }


def prepare_nestjs(work_dir: pathlib.Path, out_dir: pathlib.Path) -> Dict[str, Any]:
    """Prepare source and dependency archives for NestJS v10.3.3."""
    repo_dir = work_dir / "nest"
    if not repo_dir.exists():
        subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", NESTJS_TAG, NESTJS_REPO, str(repo_dir)],
            check=True,
        )

    commit_sha = (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_dir).decode().strip()
    )
    shutil.rmtree(repo_dir / ".git", ignore_errors=True)
    shutil.rmtree(repo_dir / "node_modules", ignore_errors=True)
    shutil.rmtree(repo_dir / "integration", ignore_errors=True)
    shutil.rmtree(repo_dir / "sample", ignore_errors=True)

    # Clean any existing build outputs in packages/
    subprocess.run(
        ["find", "packages", "(", "-name", "*.js", "-o", "-name", "*.d.ts", "-o", "-name", "*.tsbuildinfo", ")", "-delete"],
        cwd=repo_dir,
        check=False,
    )

    pkg_path = repo_dir / "package.json"
    pkg = json.loads(pkg_path.read_text())
    deps = {k: v for k, v in pkg["dependencies"].items() if k != "@nuxtjs/opencollective"}
    dev_deps = {
        k: v
        for k, v in pkg["devDependencies"].items()
        if any(k == p or k.startswith(p) for p in KEEP_NEST_DEV_PREFIXES)
    }
    dev_deps["tsconfig-paths"] = "4.2.0"
    pkg["dependencies"] = deps
    pkg["devDependencies"] = dev_deps
    pkg["scripts"].pop("postinstall", None)
    pkg["scripts"].pop("prepare", None)
    pkg_path.write_text(json.dumps(pkg, indent=2))

    # Create controlled scaling tsconfig project-reference files on the same NestJS source
    single_tsconfig = {
        "files": [],
        "references": [{"path": "./common/tsconfig.build.json"}],
    }
    (repo_dir / "packages" / "tsconfig.single.json").write_text(
        json.dumps(single_tsconfig, indent=2)
    )

    core_subset_tsconfig = {
        "files": [],
        "references": [
            {"path": "./common/tsconfig.build.json"},
            {"path": "./core/tsconfig.build.json"},
            {"path": "./platform-express/tsconfig.build.json"},
            {"path": "./testing/tsconfig.build.json"},
        ],
    }
    (repo_dir / "packages" / "tsconfig.core-subset.json").write_text(
        json.dumps(core_subset_tsconfig, indent=2)
    )

    source_archive = out_dir / "nestjs-v10.3.3-source.tar.gz"
    create_deterministic_tar_gz(repo_dir, source_archive)

    subprocess.run(
        ["npm", "install", "--ignore-scripts", "--legacy-peer-deps"],
        cwd=repo_dir,
        check=True,
    )
    deps_archive = out_dir / "nestjs-v10.3.3-deps.tar.gz"
    create_deterministic_tar_gz(repo_dir / "node_modules", deps_archive)

    return {
        "project": "nestjs",
        "version": NESTJS_TAG,
        "commit": commit_sha,
        "license": "MIT",
        "source_archive": source_archive.name,
        "source_sha256": sha256_file(source_archive),
        "source_size_bytes": source_archive.stat().st_size,
        "deps_archive": deps_archive.name,
        "deps_sha256": sha256_file(deps_archive),
        "deps_size_bytes": deps_archive.stat().st_size,
    }


PROFILES: Dict[str, Dict[str, Any]] = {
    "test": {
        "project": "ts-pattern",
        "description": "ts-pattern smoke CI (typecheck + build + 1 Jest suite)",
        "typecheck_args": ["./node_modules/typescript/bin/tsc", "--strict", "--noEmit"],
        "build_args": ["./node_modules/typescript/bin/tsc", "--outDir", "dist", "--declaration"],
        "test_runner": "jest",
        "test_args": [
            "./node_modules/jest/bin/jest.js",
            "tests/tuples.test.ts",
            "--runInBand",
            "--no-cache",
        ],
        "expected_tests_passed": 9,
        "expected_emitted_files_min": 30,
        "build_passes": 1,
    },
    "small": {
        "project": "ts-pattern",
        "description": "ts-pattern full CI (typecheck + build + all 46 Jest suites)",
        "typecheck_args": [
            "./node_modules/typescript/bin/tsc",
            "--strict",
            "--noEmit",
            "--extendedDiagnostics",
        ],
        "build_args": ["./node_modules/typescript/bin/tsc", "--outDir", "dist", "--declaration"],
        "test_runner": "jest",
        "test_args": [
            "./node_modules/jest/bin/jest.js",
            "--runInBand",
            "--no-cache",
        ],
        "expected_tests_passed": 387,
        "expected_emitted_files_min": 30,
        "build_passes": 1,
    },
    "nestjs-single": {
        "project": "nestjs",
        "description": "NestJS single package (@nestjs/common) build + unit tests",
        "typecheck_args": [
            "./node_modules/typescript/bin/tsc",
            "-p",
            "packages/common/tsconfig.build.json",
            "--noEmit",
        ],
        "build_args": [
            "./node_modules/typescript/bin/tsc",
            "-b",
            "packages/tsconfig.single.json",
        ],
        "clean_args": [
            "./node_modules/typescript/bin/tsc",
            "-b",
            "--clean",
            "packages/tsconfig.single.json",
        ],
        "test_runner": "mocha",
        "test_args": [
            "./node_modules/mocha/bin/mocha.js",
            "packages/common/test/**/*.spec.ts",
        ],
        "expected_tests_passed": 359,
        "expected_emitted_files_min": 250,
        "build_passes": 1,
    },
    "large": {
        "project": "nestjs",
        "description": "NestJS core workspace (common + core + platform-express + testing) multi-pass build + unit tests",
        "typecheck_args": [
            "./node_modules/typescript/bin/tsc",
            "-p",
            "packages/common/tsconfig.build.json",
            "--noEmit",
        ],
        "build_args": [
            "./node_modules/typescript/bin/tsc",
            "-b",
            "packages/tsconfig.core-subset.json",
        ],
        "clean_args": [
            "./node_modules/typescript/bin/tsc",
            "-b",
            "--clean",
            "packages/tsconfig.core-subset.json",
        ],
        "test_runner": "mocha",
        "test_args": [
            "./node_modules/mocha/bin/mocha.js",
            "packages/common/test/**/*.spec.ts",
            "packages/core/test/**/*.spec.ts",
        ],
        "expected_tests_passed": 931,
        "expected_emitted_files_min": 650,
        "build_passes": 2,
    },
    "nestjs-full": {
        "project": "nestjs",
        "description": "NestJS full 9-package workspace multi-pass clean build + all 1,616 unit tests",
        "typecheck_args": [
            "./node_modules/typescript/bin/tsc",
            "-p",
            "packages/common/tsconfig.build.json",
            "--noEmit",
        ],
        "build_args": [
            "./node_modules/typescript/bin/tsc",
            "-b",
            "packages/tsconfig.json",
        ],
        "clean_args": [
            "./node_modules/typescript/bin/tsc",
            "-b",
            "--clean",
            "packages/tsconfig.json",
        ],
        "test_runner": "mocha",
        "test_args": [
            "./node_modules/mocha/bin/mocha.js",
            "packages/**/*.spec.ts",
            "--ignore",
            "packages/microservices/test/json-socket/**",
        ],
        "expected_tests_passed": 1616,
        "expected_emitted_files_min": 1400,
        "build_passes": 4,
    },
    "nestjs-endurance": {
        "project": "nestjs",
        "description": "Long-running NestJS endurance CI matrix (12 clean workspace builds + full unit test runs)",
        "typecheck_args": [
            "./node_modules/typescript/bin/tsc",
            "-p",
            "packages/common/tsconfig.build.json",
            "--noEmit",
        ],
        "build_args": [
            "./node_modules/typescript/bin/tsc",
            "-b",
            "packages/tsconfig.json",
        ],
        "clean_args": [
            "./node_modules/typescript/bin/tsc",
            "-b",
            "--clean",
            "packages/tsconfig.json",
        ],
        "test_runner": "mocha",
        "test_args": [
            "./node_modules/mocha/bin/mocha.js",
            "packages/**/*.spec.ts",
            "--ignore",
            "packages/microservices/test/json-socket/**",
        ],
        "expected_tests_passed": 1616,
        "expected_emitted_files_min": 1400,
        "build_passes": 12,
        "test_passes": 4,
    },
}


def ensure_fixtures(data_dir: str | pathlib.Path, scratch_dir: str | pathlib.Path | None = None) -> Dict[str, Any]:
    """Ensure all 313.serverless-ci fixture archives and manifest exist in data_dir."""
    out_dir = pathlib.Path(data_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "fixtures_manifest.json"

    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        all_exist = True
        for proj_info in manifest.get("projects", {}).values():
            src_p = out_dir / proj_info["source_archive"]
            dep_p = out_dir / proj_info["deps_archive"]
            if not (src_p.exists() and dep_p.exists()):
                all_exist = False
                break
        if all_exist:
            manifest["profiles"] = PROFILES
            manifest_path.write_text(json.dumps(manifest, indent=2))
            return manifest

    if scratch_dir is not None and pathlib.Path(scratch_dir).exists():
        work_dir = pathlib.Path(scratch_dir)
        ts_meta = prepare_ts_pattern(work_dir, out_dir)
        nest_meta = prepare_nestjs(work_dir, out_dir)
    else:
        with tempfile.TemporaryDirectory(prefix="sebs-ci-fixtures-") as tmp:
            work_dir = pathlib.Path(tmp)
            ts_meta = prepare_ts_pattern(work_dir, out_dir)
            nest_meta = prepare_nestjs(work_dir, out_dir)

    manifest = {
        "schema_version": 1,
        "projects": {
            "ts-pattern": ts_meta,
            "nestjs": nest_meta,
        },
        "profiles": PROFILES,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return manifest


if __name__ == "__main__":
    project_root = pathlib.Path(__file__).resolve().parents[3]
    default_data_dir = project_root / "benchmarks-data" / "300.utilities" / "313.serverless-ci"
    default_scratch = pathlib.Path(
        "/home/dcem/.gemini/antigravity-cli/brain/7c55542e-d086-4cd2-b42d-9f1befd2b934/scratch"
    )
    manifest_data = ensure_fixtures(default_data_dir, default_scratch if default_scratch.exists() else None)
    print(json.dumps(manifest_data["projects"], indent=2))
