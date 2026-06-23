/**
 * Minimal Puppeteer-based sidecar for renderer stage.
 *
 * Exposes these HTTP endpoints:
 *   GET  /ping              → 200 when browser ready, 204 while warming up
 *   POST /render/grid       → body {source, options?} → raw PNG bytes (2×2 grid)
 *   POST /render/object     → body {object, options?} → raw PNG bytes (2×2 grid)
 *
 * Deliberately omits the validation pool because the js_checker stage has
 * already validated the code.
 */

import http from 'node:http';

import { startStaticServer } from './static-server.js';
import { ensureBrowser, closeBrowser } from './browser.js';
import { renderGrid, renderViews, buildParams } from './renderer.js';
import { objectPool } from './object-pool.js';

const VIEW_PRESETS = {
  // theta (azimuth, degrees from +Z axis, ccw around +Y), phi (elevation from horizon)
  front:       { theta:   0.0, phi:   0.0 },
  back:        { theta: 180.0, phi:   0.0 },
  right:       { theta:  90.0, phi:   0.0 },
  left:        { theta: 270.0, phi:   0.0 },
  top:         { theta:   0.0, phi:  89.0 },
  bottom:      { theta:   0.0, phi: -89.0 },
  perspective: { theta:  24.0, phi: -15.0 },
  // Front-hemisphere + top angles used by the multi-stage judge (S1/S3/S4).
  // phi follows sphericalToCartesian's convention (y = -radius*sin(phi)):
  // +phi views from below, -phi from above. Keep in sync with the judge labels.
  front_left:  { theta:  30.0, phi:   0.0 },
  front_right: { theta: 330.0, phi:   0.0 },
  front_below: { theta:   0.0, phi:  15.0 },
  front_above: { theta:   0.0, phi: -30.0 },
  top_down:    { theta:   0.0, phi: -90.0 },
};

const PORT = parseInt(process.env.PORT || '8003', 10);

let ready = false;
let shuttingDown = false;

async function readBody(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  return Buffer.concat(chunks).toString('utf-8');
}

function sendJson(res, status, data) {
  const body = JSON.stringify(data);
  res.writeHead(status, {
    'Content-Type': 'application/json',
    'Content-Length': Buffer.byteLength(body),
  });
  res.end(body);
}

function sendPng(res, buf) {
  res.writeHead(200, {
    'Content-Type': 'image/png',
    'Content-Length': buf.length,
  });
  res.end(buf);
}

async function handleRenderViews(req, res) {
  const t0 = Date.now();
  let body;
  try {
    body = await readBody(req);
  } catch (err) {
    sendJson(res, 400, { error: `body read failed: ${err.message}` });
    return;
  }

  let payload;
  try {
    payload = JSON.parse(body);
  } catch (err) {
    sendJson(res, 400, { error: `invalid JSON: ${err.message}` });
    return;
  }

  const source = payload.source;
  if (typeof source !== 'string' || source.length === 0) {
    sendJson(res, 400, { error: 'missing "source" string' });
    return;
  }

  // Caller can pass either custom thetas+phis (arrays) OR view names ("views": [...]).
  const viewNames = Array.isArray(payload.views)
    ? payload.views
    : ['front', 'right', 'top', 'perspective'];

  const thetas = [];
  const phis = [];
  const labels = [];
  for (const name of viewNames) {
    const preset = VIEW_PRESETS[name];
    if (!preset) {
      sendJson(res, 400, { error: `unknown view '${name}'. Known: ${Object.keys(VIEW_PRESETS).join(', ')}` });
      return;
    }
    thetas.push(preset.theta);
    phis.push(preset.phi);
    labels.push(name);
  }

  const options = { ...(payload.options || {}), thetas, phis };

  try {
    const pngBufs = await renderViews(source, options);
    const ms = Date.now() - t0;
    // Response: JSON { views: { name: base64_png, ... }, ms }
    const views = {};
    let total = 0;
    for (let i = 0; i < labels.length; i++) {
      views[labels[i]] = pngBufs[i].toString('base64');
      total += pngBufs[i].length;
    }
    console.log(`[render_runner] /render/views ok: ${labels.length} views ${total}B in ${ms}ms`);
    sendJson(res, 200, { views, ms });
  } catch (err) {
    const ms = Date.now() - t0;
    console.error(`[render_runner] /render/views fail in ${ms}ms: ${err.message}`);
    sendJson(res, 500, { error: err.message || String(err) });
  }
}

async function handleRenderGrid(req, res) {
  const t0 = Date.now();
  let body;
  try {
    body = await readBody(req);
  } catch (err) {
    sendJson(res, 400, { error: `body read failed: ${err.message}` });
    return;
  }

  let payload;
  try {
    payload = JSON.parse(body);
  } catch (err) {
    sendJson(res, 400, { error: `invalid JSON: ${err.message}` });
    return;
  }

  const source = payload.source;
  if (typeof source !== 'string' || source.length === 0) {
    sendJson(res, 400, { error: 'missing "source" string' });
    return;
  }

  const options = payload.options || {};

  try {
    const pngBuf = await renderGrid(source, options);
    const ms = Date.now() - t0;
    console.log(`[render_runner] /render/grid ok: ${pngBuf.length}B in ${ms}ms`);
    sendPng(res, pngBuf);
  } catch (err) {
    const ms = Date.now() - t0;
    console.error(`[render_runner] /render/grid fail in ${ms}ms: ${err.message}`);
    sendJson(res, 500, { error: err.message || String(err) });
  }
}

async function handleRenderObject(req, res) {
  const t0 = Date.now();
  let body;
  try {
    body = await readBody(req);
  } catch (err) {
    sendJson(res, 400, { error: `body read failed: ${err.message}` });
    return;
  }

  let payload;
  try {
    payload = JSON.parse(body);
  } catch (err) {
    sendJson(res, 400, { error: `invalid JSON: ${err.message}` });
    return;
  }

  const object = payload.object;
  if (!object || typeof object !== 'object') {
    sendJson(res, 400, { error: 'missing "object" (serialized scene_json)' });
    return;
  }

  const options = payload.options || {};

  try {
    const pngBuf = await objectPool.renderObjectGrid(object, buildParams(options));
    const ms = Date.now() - t0;
    console.log(`[render_runner] /render/object ok: ${pngBuf.length}B in ${ms}ms`);
    sendPng(res, pngBuf);
  } catch (err) {
    const ms = Date.now() - t0;
    console.error(`[render_runner] /render/object fail in ${ms}ms: ${err.message}`);
    sendJson(res, 500, { error: err.message || String(err) });
  }
}

const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, 'http://localhost');

    if (req.method === 'GET' && url.pathname === '/ping') {
      res.writeHead(ready ? 200 : 204);
      res.end();
      return;
    }

    if (req.method === 'POST' && url.pathname === '/render/grid') {
      if (!ready) {
        sendJson(res, 503, { error: 'renderer not ready' });
        return;
      }
      await handleRenderGrid(req, res);
      return;
    }

    if (req.method === 'POST' && url.pathname === '/render/views') {
      if (!ready) {
        sendJson(res, 503, { error: 'renderer not ready' });
        return;
      }
      await handleRenderViews(req, res);
      return;
    }

    if (req.method === 'POST' && url.pathname === '/render/object') {
      if (!ready) {
        sendJson(res, 503, { error: 'renderer not ready' });
        return;
      }
      await handleRenderObject(req, res);
      return;
    }

    sendJson(res, 404, { error: 'not found' });
  } catch (err) {
    console.error('[render_runner] request error:', err);
    try {
      sendJson(res, 500, { error: err.message || 'internal error' });
    } catch {}
  }
});

async function gracefulShutdown(signal) {
  if (shuttingDown) return;
  shuttingDown = true;
  console.log(`[render_runner] ${signal} received, shutting down...`);
  server.close();
  try {
    await closeBrowser();
  } catch (err) {
    console.error('[render_runner] browser close error:', err.message);
  }
  process.exit(0);
}

process.on('SIGTERM', () => gracefulShutdown('SIGTERM'));
process.on('SIGINT', () => gracefulShutdown('SIGINT'));

async function main() {
  console.log('[render_runner] starting static server...');
  await startStaticServer();

  server.listen(PORT, '127.0.0.1', () => {
    console.log(`[render_runner] listening on 127.0.0.1:${PORT}`);
  });

  console.log('[render_runner] launching browser...');
  await ensureBrowser();

  ready = true;
  console.log('[render_runner] ready');
}

main().catch((err) => {
  console.error('[render_runner] fatal startup error:', err);
  process.exit(1);
});
