import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Worker } from 'node:worker_threads';

import { parseSource } from './validator/parse.js';
import { staticAnalyze } from './validator/staticAnalysis.js';
import { filterSafeExecArgv } from './validator/execute.js';
import { compactScene } from '../renderer/render_service/scene_codec.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const WORKER_PATH = path.join(__dirname, 'serialize_worker.mjs');

const TIMEOUT_MS = 5000;
const HEAP_LIMIT_MB = 256;

function fail(stage, rule, detail) {
  return {
    passed: false,
    stagesRun: [],
    failures: [{ stage, rule, detail }],
    metrics: null,
    moduleLoadMs: null,
    executionMs: null,
    totalMs: 0,
    object: null,
  };
}

function executeSerialize(source) {
  return new Promise((resolve) => {
    const start = Date.now();
    let settled = false;

    const worker = new Worker(WORKER_PATH, {
      workerData: { source },
      execArgv: filterSafeExecArgv(process.execArgv),
      resourceLimits: { maxOldGenerationSizeMb: HEAP_LIMIT_MB },
    });

    const settle = (result) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      worker.terminate().catch(() => {});
      resolve({ ...result, totalMs: Date.now() - start });
    };

    const timer = setTimeout(() => {
      settle({
        failures: [{
          stage: 'execution',
          rule: 'TIMEOUT_EXCEEDED',
          detail: `combined module-load + generate() exceeded ${TIMEOUT_MS}ms — worker terminated`,
        }],
        metrics: null,
        moduleLoadMs: null,
        executionMs: null,
        object: null,
      });
    }, TIMEOUT_MS);

    worker.on('message', (msg) => settle(msg));

    worker.on('error', (err) => {
      const isOom =
        (err && err.code && String(err.code).includes('OUT_OF_MEMORY')) ||
        (err && err.message && /out of memory/i.test(err.message));
      settle({
        failures: [
          isOom
            ? { stage: 'execution', rule: 'HEAP_EXCEEDED', detail: `worker exceeded ${HEAP_LIMIT_MB} MB heap cap` }
            : { stage: 'execution', rule: 'EXECUTION_THREW', detail: err && err.message ? err.message : String(err) },
        ],
        metrics: null,
        moduleLoadMs: null,
        executionMs: null,
        object: null,
      });
    });

    worker.on('exit', (code) => {
      if (settled) return;
      settle({
        failures: [{ stage: 'execution', rule: 'EXECUTION_THREW', detail: `worker exited unexpectedly with code ${code}` }],
        metrics: null,
        moduleLoadMs: null,
        executionMs: null,
        object: null,
      });
    });
  });
}

async function serialize(source) {
  const { ast, failures: parseFailures } = parseSource(source);
  if (parseFailures.length > 0) {
    return { passed: false, stagesRun: ['parse'], failures: parseFailures, metrics: null, moduleLoadMs: null, executionMs: null, totalMs: 0, object: null };
  }

  const staticFailures = staticAnalyze(ast);
  if (staticFailures.length > 0) {
    return { passed: false, stagesRun: ['parse', 'static_analysis'], failures: staticFailures, metrics: null, moduleLoadMs: null, executionMs: null, totalMs: 0, object: null };
  }

  const exportDecl = ast.program.body.find(s => s.type === 'ExportDefaultDeclaration');
  const transformed = source.slice(0, exportDecl.start) + 'return ' + source.slice(exportDecl.declaration.start);

  const workerResult = await executeSerialize(transformed);
  const passed = workerResult.failures.length === 0;

  let stagesRun;
  if (passed) {
    stagesRun = ['parse', 'static_analysis', 'module_load', 'execution', 'post_validation'];
  } else {
    const deepest = workerResult.failures[workerResult.failures.length - 1].stage;
    const worker_stages = ['module_load', 'execution', 'post_validation'];
    const idx = worker_stages.indexOf(deepest);
    stagesRun = ['parse', 'static_analysis', ...worker_stages.slice(0, idx + 1)];
  }

  let object = passed ? workerResult.object : null;
  if (object) {
    object = compactScene(object);
  }

  return {
    passed,
    stagesRun,
    failures: workerResult.failures,
    metrics: workerResult.metrics,
    moduleLoadMs: workerResult.moduleLoadMs,
    executionMs: workerResult.executionMs,
    totalMs: workerResult.totalMs,
    object,
  };
}

const codePath = process.argv[2];
let result;
if (!codePath) {
  result = fail('runner', 'NO_INPUT', 'no source file path provided');
} else {
  try {
    const source = fs.readFileSync(codePath, 'utf8');
    result = await serialize(source);
  } catch (err) {
    result = fail('runner', 'EXECUTION_THREW', String(err.message || err));
  }
}

process.stdout.write(JSON.stringify(result), () => process.exit(0));
