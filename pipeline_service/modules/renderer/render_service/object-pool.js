import { ensureBrowser, isBrowserHealthy, restartBrowser } from './browser.js';
import { STATIC_PORT } from './static-server.js';

const RENDER_TIMEOUT_MS = parseInt(process.env.RENDER_TIMEOUT_MS || '30000', 10);
const POOL_SIZE = parseInt(process.env.RENDER_POOL_SIZE || '1', 10);
const PAGE_URL = `http://127.0.0.1:${STATIC_PORT}/object-page.html`;

class ObjectPool {
  constructor() {
    this._slots = Array.from({ length: POOL_SIZE }, () => ({ page: null, context: null }));
    this._free = this._slots.map((_, i) => i);
    this._waiting = [];
  }

  async renderObjectGrid(object, params) {
    const idx = await this._acquire();
    try {
      const dataUrl = await this._renderInSlot(idx, object, params);
      return dataUrlToBuffer(dataUrl);
    } finally {
      this._release(idx);
    }
  }

  _acquire() {
    if (this._free.length > 0) return Promise.resolve(this._free.pop());
    return new Promise((resolve) => this._waiting.push(resolve));
  }

  _release(idx) {
    if (this._waiting.length > 0) {
      this._waiting.shift()(idx);
    } else {
      this._free.push(idx);
    }
  }

  async _ensureSlotPage(slot) {
    if (slot.page && !slot.page.isClosed()) return slot.page;

    await this._disposeSlot(slot);
    if (!isBrowserHealthy()) {
      console.log('[object-pool] browser not healthy, restarting...');
      await restartBrowser();
    }
    const browser = await ensureBrowser();
    const context = await browser.createBrowserContext();
    const page = await context.newPage();
    page.on('pageerror', (err) => console.error(`[object page error] ${err.message}`));
    await page.goto(PAGE_URL, { waitUntil: 'domcontentloaded' });
    await page.waitForFunction('window.__ready === true', { timeout: 10_000 });
    await page.setOfflineMode(true);
    slot.context = context;
    slot.page = page;
    return page;
  }

  async _disposeSlot(slot) {
    if (slot.context) {
      try { await slot.context.close(); } catch {}
    }
    slot.context = null;
    slot.page = null;
  }

  async _renderInSlot(idx, object, params) {
    const slot = this._slots[idx];
    try {
      const page = await this._ensureSlotPage(slot);
      const result = await withTimeout(
        page.evaluate((json, p) => window.__renderObject(json, p), object, params),
        RENDER_TIMEOUT_MS,
      );
      if (result.error) throw new Error(result.error);
      return result.data;
    } catch (err) {
      await this._disposeSlot(slot);
      if (!isBrowserHealthy()) {
        console.log('[object-pool] browser died during render, will restart on next request');
      }
      throw err;
    }
  }
}

function withTimeout(promise, ms) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`render timed out after ${ms}ms`)), ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

function dataUrlToBuffer(dataUrl) {
  const base64 = dataUrl.replace(/^data:image\/png;base64,/, '');
  return Buffer.from(base64, 'base64');
}

export const objectPool = new ObjectPool();
