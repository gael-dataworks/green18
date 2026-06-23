/**
 * Worker-thread runner for miner code.
 *
 * Loaded inside a Node.js worker_thread by execute.js. Compiles miner source
 * via `new Function(body)` (no ESM loader, no module cache), installs runtime
 * traps in a single phase, runs `generate(THREE)`, and post-validates the
 * result.
 *
 * Source arrives here ALREADY TRANSFORMED — `export default <decl>` replaced
 * with `return <decl>` by validator/index.js using exact AST byte offsets.
 * This lets us drop ESM entirely: no temp file, no `import()`, no module
 * cache growth. Because there is no ESM loader involvement, ALL runtime traps
 * (including Function and eval) are installed before any miner code runs.
 */

import { parentPort, workerData } from 'node:worker_threads';
import * as THREE from 'three';
import { postValidate } from './postValidation.js';

const SafeFunction = Function;
// Capture Date.now BEFORE any traps are installed so worker-internal timing
// measurements keep working after `Date` is trapped in globalThis.
const NowMs = Date.now.bind(Date);

const TRAPPED_GLOBALS = [
  'setTimeout', 'setInterval', 'setImmediate', 'queueMicrotask',
  'fetch', 'XMLHttpRequest', 'WebSocket',
  'crypto',
  'Date', 'performance',
  'eval', 'Function',
  'process', 'global',
  'WeakRef', 'FinalizationRegistry',
  'SharedArrayBuffer', 'Atomics',
];

const CODEGEN_PROTOTYPES = [
  SafeFunction.prototype,
  Object.getPrototypeOf(function*(){}),
  Object.getPrototypeOf(async function(){}),
  Object.getPrototypeOf(async function*(){}),
];

function trapViolation(name) {
  throw new Error(`Runtime violation: ${name} is forbidden`);
}

function send(msg) {
  parentPort.postMessage(msg);
}

function run() {
  const { source } = workerData;

  const origRandom = Math.random;
  const savedGlobals = new Map();
  const savedCtors = [];

  let moduleLoadMs = 0;
  let executionMs = 0;
  const moduleStart = NowMs();

  try {
    // Deterministic Math.random — same seed/stream as upstream validator.
    let seed = 0x12345678;
    Math.random = () => {
      seed |= 0; seed = seed + 0x6D2B79F5 | 0;
      let t = Math.imul(seed ^ seed >>> 15, 1 | seed);
      t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
      return ((t ^ t >>> 14) >>> 0) / 4294967296;
    };

    for (const name of TRAPPED_GLOBALS) {
      if (!(name in globalThis)) continue;
      savedGlobals.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
      try {
        Object.defineProperty(globalThis, name, {
          get() { trapViolation(name); },
          configurable: true,
        });
      } catch {
        try { globalThis[name] = undefined; } catch {}
      }
    }

    for (const proto of CODEGEN_PROTOTYPES) {
      const desc = Object.getOwnPropertyDescriptor(proto, 'constructor');
      savedCtors.push({ proto, desc });
      Object.defineProperty(proto, 'constructor', {
        get() { trapViolation('Function constructor'); },
        configurable: true,
      });
    }

    const body = `'use strict';\n${source}`;
    let factory;
    try {
      factory = new SafeFunction(body);
    } catch (err) {
      moduleLoadMs = NowMs() - moduleStart;
      send({
        failures: [{
          stage: 'module_load',
          rule: 'EXECUTION_THREW',
          detail: `compilation failed: ${err && err.message ? err.message : String(err)}`,
        }],
        metrics: null,
        moduleLoadMs,
        executionMs: 0,
      });
      return;
    }
    moduleLoadMs = NowMs() - moduleStart;

    const execStart = NowMs();
    let generate;
    try {
      generate = factory();
    } catch (err) {
      executionMs = NowMs() - execStart;
      const rule = err && err.message && err.message.includes('Runtime violation')
        ? 'RUNTIME_VIOLATION'
        : 'EXECUTION_THREW';
      send({
        failures: [{
          stage: 'module_load',
          rule,
          detail: err && err.message ? err.message : String(err),
        }],
        metrics: null,
        moduleLoadMs,
        executionMs,
      });
      return;
    }

    if (typeof generate !== 'function') {
      executionMs = NowMs() - execStart;
      send({
        failures: [{
          stage: 'module_load',
          rule: 'INVALID_RETURN_TYPE',
          detail: `default export is not a function (got ${typeof generate})`,
        }],
        metrics: null,
        moduleLoadMs,
        executionMs,
      });
      return;
    }

    // Call generate() synchronously and capture the raw return value BEFORE
    // any await. If we awaited here, a Promise return value would be unwrapped
    // and the thenable check below would never fire.
    let rawResult;
    try {
      rawResult = generate(THREE);
    } catch (err) {
      executionMs = NowMs() - execStart;
      const rule = err && err.message && err.message.includes('Runtime violation')
        ? 'RUNTIME_VIOLATION'
        : 'EXECUTION_THREW';
      send({
        failures: [{
          stage: 'execution',
          rule,
          detail: err && err.message ? err.message : String(err),
        }],
        metrics: null,
        moduleLoadMs,
        executionMs,
      });
      return;
    }
    executionMs = NowMs() - execStart;

    // Reject any thenable — real Promise, duck-typed, or chained.
    if (
      rawResult !== null &&
      (typeof rawResult === 'object' || typeof rawResult === 'function') &&
      typeof rawResult.then === 'function'
    ) {
      send({
        failures: [{
          stage: 'execution',
          rule: 'ASYNC_NOT_ALLOWED',
          detail: 'generate() returned a Promise or thenable',
        }],
        metrics: null,
        moduleLoadMs,
        executionMs,
      });
      return;
    }

    let postResult;
    try {
      postResult = postValidate(rawResult);
    } catch (err) {
      send({
        failures: [{
          stage: 'post_validation',
          rule: 'EXECUTION_THREW',
          detail: err && err.message ? err.message : String(err),
        }],
        metrics: null,
        moduleLoadMs,
        executionMs,
      });
      return;
    }

    send({
      failures: postResult.failures,
      metrics: postResult.metrics,
      moduleLoadMs,
      executionMs,
    });
  } finally {
    Math.random = origRandom;
    for (const [name, desc] of savedGlobals) {
      try { Object.defineProperty(globalThis, name, desc); }
      catch { try { globalThis[name] = desc?.value; } catch {} }
    }
    for (const { proto, desc } of savedCtors) {
      try { Object.defineProperty(proto, 'constructor', desc); }
      catch {}
    }
  }
}

try {
  run();
} catch (err) {
  send({
    failures: [{
      stage: 'execution',
      rule: 'EXECUTION_THREW',
      detail: err && err.stack ? err.stack : String(err),
    }],
    metrics: null,
    moduleLoadMs: 0,
    executionMs: 0,
  });
}
