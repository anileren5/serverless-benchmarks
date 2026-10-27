const child_process = require('child_process');
const crypto = require('crypto');
const fs = require('fs');
const os = require('os');
const path = require('path');
const storage = require('./storage');

let tar = null;
try {
  tar = require('tar');
} catch (_err) {
  tar = null;
}

let storage_handler = new storage.storage();

const FAILURE_CLASSES = {
  OUT_OF_SPACE: 'OUT_OF_SPACE',
  OUT_OF_MEMORY: 'OUT_OF_MEMORY',
  TIMEOUT: 'TIMEOUT',
  BUILD_FAILURE: 'BUILD_FAILURE',
  TEST_FAILURE: 'TEST_FAILURE',
  TOOLCHAIN_ERROR: 'TOOLCHAIN_ERROR'
};

function nowMicros() {
  return Number(process.hrtime.bigint() / 1000n);
}

function computeFileSha256(filePath) {
  const hash = crypto.createHash('sha256');
  const data = fs.readFileSync(filePath);
  hash.update(data);
  return hash.digest('hex');
}

function walkFilesSorted(rootDir, predicate) {
  const results = [];
  function visit(currentDir) {
    if (!fs.existsSync(currentDir)) {
      return;
    }
    const entries = fs.readdirSync(currentDir, { withFileTypes: true });
    entries.sort((a, b) => a.name.localeCompare(b.name));
    for (const entry of entries) {
      if (entry.name === 'node_modules' || entry.name === '.git') {
        continue;
      }
      const fullPath = path.join(currentDir, entry.name);
      if (entry.isDirectory()) {
        visit(fullPath);
      } else if (entry.isFile() && predicate(fullPath, entry.name)) {
        results.push(fullPath);
      }
    }
  }
  visit(rootDir);
  return results;
}

function computeBuildOutputFingerprint(workspaceDir, outputDirs) {
  const hash = crypto.createHash('sha256');
  let emittedFilesCount = 0;
  let emittedBytes = 0;

  const dirsToScan = Array.isArray(outputDirs) && outputDirs.length > 0
    ? outputDirs.map((d) => path.join(workspaceDir, d))
    : [workspaceDir];

  for (const dirPath of dirsToScan) {
    const files = walkFilesSorted(dirPath, (fullPath, name) => {
      if (outputDirs && outputDirs.length > 0) {
        return name.endsWith('.js') || name.endsWith('.d.ts') || name.endsWith('.cjs') || name.endsWith('.mjs');
      }
      const rel = path.relative(workspaceDir, fullPath);
      return (
        rel.startsWith('packages' + path.sep) &&
        !rel.includes(path.sep + 'test' + path.sep) &&
        (name.endsWith('.js') || name.endsWith('.d.ts'))
      );
    });

    for (const filePath of files) {
      const relPath = path.relative(workspaceDir, filePath).split(path.sep).join('/');
      const content = fs.readFileSync(filePath);
      hash.update(relPath + '\0');
      hash.update(content);
      hash.update('\0');
      emittedFilesCount += 1;
      emittedBytes += content.length;
    }
  }

  return {
    build_output_sha256: hash.digest('hex'),
    emitted_files_count: emittedFilesCount,
    emitted_bytes: emittedBytes
  };
}

function getTmpDiskStats(tmpRoot = '/tmp') {
  try {
    if (typeof fs.statfsSync === 'function') {
      const stats = fs.statfsSync(tmpRoot);
      const totalBytes = stats.blocks * stats.bsize;
      const freeBytes = stats.bavail * stats.bsize;
      const usedBytes = totalBytes - freeBytes;
      return { total_bytes: totalBytes, free_bytes: freeBytes, used_bytes: usedBytes };
    }
  } catch (_err) {
    // Fallback below
  }
  return { total_bytes: 0, free_bytes: 0, used_bytes: 0 };
}

function getDirectorySizeBytes(dirPath) {
  let total = 0;
  if (!fs.existsSync(dirPath)) {
    return 0;
  }
  const stack = [dirPath];
  while (stack.length > 0) {
    const current = stack.pop();
    let entries = [];
    try {
      entries = fs.readdirSync(current, { withFileTypes: true });
    } catch (_err) {
      continue;
    }
    for (const entry of entries) {
      const fullPath = path.join(current, entry.name);
      try {
        if (entry.isSymbolicLink()) {
          continue;
        } else if (entry.isDirectory()) {
          stack.push(fullPath);
        } else if (entry.isFile()) {
          total += fs.statSync(fullPath).size;
        }
      } catch (_err) {
        // Ignore transient files
      }
    }
  }
  return total;
}

function sampleProcessMemoryBytes(childPid) {
  let rssBytes = process.memoryUsage().rss;
  if (childPid) {
    try {
      const statusText = fs.readFileSync(`/proc/${childPid}/status`, 'utf8');
      const hwmMatch = statusText.match(/VmHWM:\s+(\d+)\s+kB/);
      const rssMatch = statusText.match(/VmRSS:\s+(\d+)\s+kB/);
      const childKb = hwmMatch ? parseInt(hwmMatch[1], 10) : (rssMatch ? parseInt(rssMatch[1], 10) : 0);
      rssBytes += childKb * 1024;
    } catch (_err) {
      // Child process may have exited or /proc may be unavailable
    }
  }
  return rssBytes;
}

async function extractTarGz(archivePath, destDir) {
  fs.mkdirSync(destDir, { recursive: true });
  if (tar && typeof tar.x === 'function') {
    await tar.x({ file: archivePath, cwd: destDir });
    return;
  }
  await new Promise((resolve, reject) => {
    const proc = child_process.spawn('tar', ['-xzf', archivePath, '-C', destDir], {
      stdio: ['ignore', 'pipe', 'pipe']
    });
    let stderr = '';
    proc.stderr.on('data', (chunk) => {
      stderr += chunk.toString();
    });
    proc.on('error', reject);
    proc.on('close', (code) => {
      if (code === 0) {
        resolve();
      } else {
        reject(new Error(`tar exited with code ${code}: ${stderr}`));
      }
    });
  });
}

function buildChildEnv() {
  const env = Object.assign({}, process.env);
  const nodeMajor = parseInt(process.versions.node.split('.')[0], 10);
  if (nodeMajor >= 22) {
    const existing = env.NODE_OPTIONS || '';
    if (!existing.includes('--no-experimental-strip-types')) {
      env.NODE_OPTIONS = `${existing} --no-experimental-strip-types`.trim();
    }
  }
  env.CI = 'true';
  env.FORCE_COLOR = '0';
  return env;
}

function runNodeTool(workspaceDir, args, timeoutMs, resourceTracker, stageName) {
  return new Promise((resolve, reject) => {
    const startUs = nowMicros();
    const child = child_process.spawn(process.execPath, args, {
      cwd: workspaceDir,
      env: buildChildEnv(),
      stdio: ['ignore', 'pipe', 'pipe']
    });

    let stdout = '';
    let stderr = '';
    let timedOut = false;

    const sampleInterval = setInterval(() => {
      const mem = sampleProcessMemoryBytes(child.pid);
      if (mem > resourceTracker.peak_rss_bytes) {
        resourceTracker.peak_rss_bytes = mem;
      }
    }, 100);

    const timer = setTimeout(() => {
      timedOut = true;
      child.kill('SIGKILL');
    }, timeoutMs);

    child.stdout.on('data', (chunk) => {
      stdout += chunk.toString();
      if (stdout.length > 128 * 1024) {
        stdout = stdout.slice(-128 * 1024);
      }
    });

    child.stderr.on('data', (chunk) => {
      stderr += chunk.toString();
      if (stderr.length > 128 * 1024) {
        stderr = stderr.slice(-128 * 1024);
      }
    });

    child.on('error', (err) => {
      clearInterval(sampleInterval);
      clearTimeout(timer);
      const error = new Error(`[${stageName}] Failed to spawn node tool: ${err.message}`);
      error.failureClass = FAILURE_CLASSES.TOOLCHAIN_ERROR;
      error.stage = stageName;
      reject(error);
    });

    child.on('close', (code, signal) => {
      const finalMem = sampleProcessMemoryBytes(child.pid);
      if (finalMem > resourceTracker.peak_rss_bytes) {
        resourceTracker.peak_rss_bytes = finalMem;
      }
      clearInterval(sampleInterval);
      clearTimeout(timer);
      const durationUs = nowMicros() - startUs;

      if (timedOut) {
        const error = new Error(`[${stageName}] Timed out after ${timeoutMs} ms`);
        error.failureClass = FAILURE_CLASSES.TIMEOUT;
        error.stage = stageName;
        error.stdout = stdout;
        error.stderr = stderr;
        return reject(error);
      }

      if (signal === 'SIGKILL' || code === 137 || /JavaScript heap out of memory|Allocation failed/i.test(stderr + stdout)) {
        const error = new Error(`[${stageName}] Process terminated due to out-of-memory (code=${code}, signal=${signal})`);
        error.failureClass = FAILURE_CLASSES.OUT_OF_MEMORY;
        error.stage = stageName;
        error.stdout = stdout;
        error.stderr = stderr;
        return reject(error);
      }

      if (/ENOSPC|no space left on device/i.test(stderr + stdout)) {
        const error = new Error(`[${stageName}] Out of disk space in /tmp`);
        error.failureClass = FAILURE_CLASSES.OUT_OF_SPACE;
        error.stage = stageName;
        error.stdout = stdout;
        error.stderr = stderr;
        return reject(error);
      }

      if (code !== 0) {
        const failureClass = stageName === 'test'
          ? FAILURE_CLASSES.TEST_FAILURE
          : FAILURE_CLASSES.BUILD_FAILURE;
        const error = new Error(`[${stageName}] Command exited with code ${code}: ${(stderr || stdout).slice(-1000)}`);
        error.failureClass = failureClass;
        error.stage = stageName;
        error.exitCode = code;
        error.stdout = stdout;
        error.stderr = stderr;
        return reject(error);
      }

      resolve({
        durationUs,
        stdout,
        stderr
      });
    });
  });
}

function parseTestCounts(outputText) {
  let passed = 0;
  let failed = 0;

  // Jest summary format: "Tests:       1 failed, 386 passed, 387 total" or "Tests:       9 passed, 9 total"
  const jestLines = outputText.match(/Tests:\s+[^\r\n]+/g) || [];
  if (jestLines.length > 0) {
    const lastLine = jestLines[jestLines.length - 1];
    const passMatch = lastLine.match(/(\d+)\s+passed/);
    const failMatch = lastLine.match(/(\d+)\s+failed/);
    if (passMatch) {
      passed = parseInt(passMatch[1], 10);
    }
    if (failMatch) {
      failed = parseInt(failMatch[1], 10);
    }
    return { passed, failed, total: passed + failed };
  }

  // Mocha summary format: "931 passing (3s)" and optional "2 failing"
  const mochaPass = outputText.match(/(\d+)\s+passing/);
  const mochaFail = outputText.match(/(\d+)\s+failing/);
  if (mochaPass) {
    passed = parseInt(mochaPass[1], 10);
  }
  if (mochaFail) {
    failed = parseInt(mochaFail[1], 10);
  }
  return { passed, failed, total: passed + failed };
}

function applyIncrementalMutation(workspaceDir, project) {
  let targetFile = null;
  if (project === 'ts-pattern') {
    targetFile = path.join(workspaceDir, 'src', 'index.ts');
  } else {
    targetFile = path.join(workspaceDir, 'packages', 'common', 'index.ts');
  }
  if (fs.existsSync(targetFile)) {
    const marker = `\n// sebs-incremental-build-marker: ${Date.now()}\n`;
    fs.appendFileSync(targetFile, marker, 'utf8');
    const now = new Date();
    fs.utimesSync(targetFile, now, now);
    return path.relative(workspaceDir, targetFile);
  }
  return null;
}

exports.handler = async function(event) {
  const totalStartUs = nowMicros();
  const bucket = event.bucket.bucket;
  const inputPrefix = event.bucket.input;
  const outputPrefix = event.bucket.output;

  const cfg = event.object || {};
  const profileName = cfg.profile || 'test';
  const project = cfg.project || 'ts-pattern';
  const version = cfg.version || 'v5.0.5';
  const sourceArchive = cfg.source_archive;
  const depsArchive = cfg.deps_archive;
  const expectedSourceSha = cfg.source_sha256;
  const expectedDepsSha = cfg.deps_sha256;
  const typecheckArgs = cfg.typecheck_args || [];
  const buildArgs = cfg.build_args || [];
  const cleanArgs = cfg.clean_args || [];
  const testArgs = cfg.test_args || [];
  const buildPasses = Math.max(1, parseInt(cfg.build_passes || 1, 10));
  const testPasses = Math.max(1, parseInt(cfg.test_passes || 1, 10));
  const reuseCache = Boolean(cfg.reuse_cache);
  const incrementalChange = Boolean(cfg.incremental_change);
  const stageTimeoutMs = parseInt(cfg.stage_timeout_ms || 840000, 10);
  const outputDirs = cfg.output_dirs || (project === 'ts-pattern' ? ['dist'] : []);

  const resourceTracker = {
    peak_rss_bytes: sampleProcessMemoryBytes(null),
    peak_tmp_bytes: 0
  };

  const cacheRoot = path.join('/tmp', 'sebs-ci-cache', `${project}-${version}`);
  const runId = crypto.randomBytes(6).toString('hex');
  const workspaceDir = reuseCache
    ? path.join(cacheRoot, 'workspace')
    : path.join('/tmp', `sebs-ci-work-${runId}`);
  const archiveTmpDir = path.join('/tmp', `sebs-ci-archives-${runId}`);

  let downloadTimeUs = 0;
  let extractTimeUs = 0;
  let depsPrepTimeUs = 0;
  let typecheckTimeUs = 0;
  let compileTimeUs = 0;
  let testTimeUs = 0;
  let uploadTimeUs = 0;
  let cacheHit = false;
  let mutatedFile = null;

  try {
    const cachedReadyMarker = path.join(workspaceDir, '.sebs_ci_ready');
    if (reuseCache && fs.existsSync(cachedReadyMarker)) {
      cacheHit = true;
      const depsPrepStart = nowMicros();
      if (incrementalChange) {
        mutatedFile = applyIncrementalMutation(workspaceDir, project);
      } else if (cleanArgs.length > 0) {
        await runNodeTool(workspaceDir, cleanArgs, stageTimeoutMs, resourceTracker, 'clean');
      }
      depsPrepTimeUs = nowMicros() - depsPrepStart;
    } else {
      if (!reuseCache && fs.existsSync(workspaceDir)) {
        fs.rmSync(workspaceDir, { recursive: true, force: true });
      }
      fs.mkdirSync(archiveTmpDir, { recursive: true });
      fs.mkdirSync(workspaceDir, { recursive: true });

      // Stage 1: Download & verify source and dependency archives from object storage
      const downloadStartUs = nowMicros();
      const localSourceArchive = path.join(archiveTmpDir, sourceArchive);
      const localDepsArchive = path.join(archiveTmpDir, depsArchive);

      await storage_handler.download(bucket, path.posix.join(inputPrefix, sourceArchive), localSourceArchive);
      await storage_handler.download(bucket, path.posix.join(inputPrefix, depsArchive), localDepsArchive);

      const actualSourceSha = computeFileSha256(localSourceArchive);
      const actualDepsSha = computeFileSha256(localDepsArchive);

      if (expectedSourceSha && actualSourceSha !== expectedSourceSha) {
        const err = new Error(`Source SHA-256 mismatch: expected ${expectedSourceSha}, got ${actualSourceSha}`);
        err.failureClass = FAILURE_CLASSES.TOOLCHAIN_ERROR;
        err.stage = 'download';
        throw err;
      }
      if (expectedDepsSha && actualDepsSha !== expectedDepsSha) {
        const err = new Error(`Deps SHA-256 mismatch: expected ${expectedDepsSha}, got ${actualDepsSha}`);
        err.failureClass = FAILURE_CLASSES.TOOLCHAIN_ERROR;
        err.stage = 'download';
        throw err;
      }
      downloadTimeUs = nowMicros() - downloadStartUs;

      // Stage 2: Extract source tree & offline dependency bundle into /tmp workspace
      const extractStartUs = nowMicros();
      await extractTarGz(localSourceArchive, workspaceDir);
      extractTimeUs = nowMicros() - extractStartUs;

      const depsStartUs = nowMicros();
      await extractTarGz(localDepsArchive, path.join(workspaceDir, 'node_modules'));
      depsPrepTimeUs = nowMicros() - depsStartUs;

      // Free archive tarballs immediately after extraction to minimize /tmp footprint
      fs.rmSync(archiveTmpDir, { recursive: true, force: true });

      if (reuseCache) {
        fs.writeFileSync(cachedReadyMarker, JSON.stringify({
          project,
          version,
          cached_at: new Date().toISOString()
        }));
      }
    }

    // Stage 3: Typechecking & Compilation
    if (typecheckArgs.length > 0) {
      const tcRes = await runNodeTool(workspaceDir, typecheckArgs, stageTimeoutMs, resourceTracker, 'typecheck');
      typecheckTimeUs += tcRes.durationUs;
    }

    let buildStdout = '';
    for (let pass = 0; pass < buildPasses; pass++) {
      if (pass > 0 && cleanArgs.length > 0 && !incrementalChange) {
        const cleanRes = await runNodeTool(workspaceDir, cleanArgs, stageTimeoutMs, resourceTracker, 'clean');
        compileTimeUs += cleanRes.durationUs;
      }
      if (buildArgs.length > 0) {
        const buildRes = await runNodeTool(workspaceDir, buildArgs, stageTimeoutMs, resourceTracker, 'compile');
        compileTimeUs += buildRes.durationUs;
        buildStdout = buildRes.stdout;
      }
    }

    const fingerprint = computeBuildOutputFingerprint(workspaceDir, outputDirs);
    const workspaceBytes = getDirectorySizeBytes(workspaceDir);
    if (workspaceBytes > resourceTracker.peak_tmp_bytes) {
      resourceTracker.peak_tmp_bytes = workspaceBytes;
    }

    // Stage 4: Unit test execution
    let testsPassed = 0;
    let testsFailed = 0;
    let testsTotal = 0;
    let testStdoutTail = '';

    for (let pass = 0; pass < testPasses; pass++) {
      if (testArgs.length > 0) {
        const testRes = await runNodeTool(workspaceDir, testArgs, stageTimeoutMs, resourceTracker, 'test');
        testTimeUs += testRes.durationUs;
        const parsed = parseTestCounts(testRes.stdout + '\n' + testRes.stderr);
        if (pass === 0) {
          testsPassed = parsed.passed;
          testsFailed = parsed.failed;
          testsTotal = parsed.total;
        }
        testStdoutTail = (testRes.stdout + '\n' + testRes.stderr).slice(-2048);
      }
    }

    // Stage 5: Upload build & test log artifact to object storage
    const uploadStartUs = nowMicros();
    const logReport = {
      status: 'SUCCESS',
      profile: profileName,
      project,
      version,
      node_version: process.version,
      arch: os.arch(),
      cache_hit: cacheHit,
      incremental_change: incrementalChange,
      mutated_file: mutatedFile,
      build_passes: buildPasses,
      test_passes: testPasses,
      build_output_sha256: fingerprint.build_output_sha256,
      emitted_files_count: fingerprint.emitted_files_count,
      emitted_bytes: fingerprint.emitted_bytes,
      tests_passed: testsPassed,
      tests_failed: testsFailed,
      tests_total: testsTotal,
      build_stdout_tail: buildStdout.slice(-1024),
      test_stdout_tail: testStdoutTail
    };

    const reportFilename = `ci-report-${project}-${profileName}-${runId}.json`;
    const localReportPath = path.join('/tmp', reportFilename);
    fs.writeFileSync(localReportPath, JSON.stringify(logReport, null, 2));
    const requestedKey = path.posix.join(outputPrefix, reportFilename);
    const uploadRet = storage_handler.upload(bucket, requestedKey, localReportPath);
    let uploadedKey = requestedKey;
    if (Array.isArray(uploadRet)) {
      uploadedKey = uploadRet[0];
      await uploadRet[1];
    } else {
      await uploadRet;
    }
    fs.rmSync(localReportPath, { force: true });
    uploadTimeUs = nowMicros() - uploadStartUs;

    const tmpStats = getTmpDiskStats('/tmp');
    const totalTimeUs = nowMicros() - totalStartUs;

    if (!reuseCache && fs.existsSync(workspaceDir)) {
      fs.rmSync(workspaceDir, { recursive: true, force: true });
    }

    return {
      result: {
        status: 'SUCCESS',
        failure_class: null,
        profile: profileName,
        project,
        version,
        cache_hit: cacheHit,
        incremental_change: incrementalChange,
        build_passes: buildPasses,
        test_passes: testPasses,
        source_sha256: expectedSourceSha,
        deps_sha256: expectedDepsSha,
        build_output_sha256: fingerprint.build_output_sha256,
        emitted_files_count: fingerprint.emitted_files_count,
        emitted_bytes: fingerprint.emitted_bytes,
        tests_passed: testsPassed,
        tests_failed: testsFailed,
        tests_total: testsTotal,
        report_key: uploadedKey
      },
      measurement: {
        download_time: downloadTimeUs,
        extract_time: extractTimeUs,
        deps_prep_time: depsPrepTimeUs,
        typecheck_time: typecheckTimeUs,
        compile_time: compileTimeUs,
        test_time: testTimeUs,
        upload_time: uploadTimeUs,
        total_time: totalTimeUs,
        peak_rss_bytes: resourceTracker.peak_rss_bytes,
        peak_tmp_bytes: resourceTracker.peak_tmp_bytes,
        tmp_free_bytes: tmpStats.free_bytes
      }
    };
  } catch (err) {
    if (fs.existsSync(archiveTmpDir)) {
      fs.rmSync(archiveTmpDir, { recursive: true, force: true });
    }
    if (!reuseCache && fs.existsSync(workspaceDir)) {
      fs.rmSync(workspaceDir, { recursive: true, force: true });
    }

    let failureClass = err.failureClass || FAILURE_CLASSES.TOOLCHAIN_ERROR;
    if (/ENOSPC|no space left on device/i.test(err.message)) {
      failureClass = FAILURE_CLASSES.OUT_OF_SPACE;
    }

    return {
      result: {
        status: 'FAILURE',
        failure_class: failureClass,
        failed_stage: err.stage || 'unknown',
        error_message: err.message,
        profile: profileName,
        project,
        version
      },
      measurement: {
        download_time: downloadTimeUs,
        extract_time: extractTimeUs,
        deps_prep_time: depsPrepTimeUs,
        typecheck_time: typecheckTimeUs,
        compile_time: compileTimeUs,
        test_time: testTimeUs,
        upload_time: uploadTimeUs,
        total_time: nowMicros() - totalStartUs,
        peak_rss_bytes: resourceTracker.peak_rss_bytes,
        peak_tmp_bytes: resourceTracker.peak_tmp_bytes
      }
    };
  }
};
