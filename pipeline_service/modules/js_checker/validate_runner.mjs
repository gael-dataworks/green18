import { validate } from './validator/index.js';
import fs from 'node:fs';

const codePath = process.argv[2];
if (!codePath) {
  process.stdout.write(JSON.stringify({
    passed: false,
    stagesRun: [],
    failures: [{ stage: 'runner', rule: 'NO_INPUT', detail: 'no source file path provided' }],
    metrics: null,
    moduleLoadMs: null,
    executionMs: null,
    totalMs: 0,
  }));
  process.exit(0);
}

let source;
try {
  source = fs.readFileSync(codePath, 'utf8');
} catch (err) {
  process.stdout.write(JSON.stringify({
    passed: false,
    stagesRun: [],
    failures: [{ stage: 'runner', rule: 'EXECUTION_THREW', detail: String(err.message || err) }],
    metrics: null,
    moduleLoadMs: null,
    executionMs: null,
    totalMs: 0,
  }));
  process.exit(0);
}

try {
  const result = await validate(source);
  process.stdout.write(JSON.stringify(result));
} catch (err) {
  process.stdout.write(JSON.stringify({
    passed: false,
    stagesRun: [],
    failures: [{ stage: 'runner', rule: 'EXECUTION_THREW', detail: String(err.message || err) }],
    metrics: null,
    moduleLoadMs: null,
    executionMs: null,
    totalMs: 0,
  }));
}
process.exit(0);
