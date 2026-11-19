// src/evaluators/visual/browserPool.js
/**
 * Browser Pool Manager
 * 
 * Instead of launching a new browser for each evaluation,
 * reuse a pool of browsers. This saves memory and startup time.
 * 
 * BEFORE: New browser per job = 2-3 sec startup × 200 jobs = 400+ seconds
 * AFTER: Pool of 3 browsers reused = Initial 10 sec + concurrent usage
 */

import { chromium } from 'playwright';
import logger from '../../config/logger.js';

class BrowserPool {
  constructor(poolSize = (parseInt(process.env.BROWSER_POOL_SIZE, 10) || 2)) {
    this.poolSize = poolSize;
    this.browsers = [];
    this.available = [];
    this.isInitialized = false;
    this.currentlyUsing = new Set();
    this.initPromise = null;
  }

  /**
   * Launch a single hardened headless Chromium.
   * NOTE: `--single-process` removed (V-12) — it makes Chromium crash-prone
   * under concurrency. Memory is controlled via pool size instead.
   */
  async _launch() {
    return chromium.launch({
      headless: true,
      args: [
        '--no-sandbox',
        '--disable-setuid-sandbox',
        '--disable-dev-shm-usage'
      ]
    });
  }

  /**
   * Replace a dead browser so the pool keeps its size (V-15).
   * Fire-and-forget safe: errors are logged, never thrown to callers.
   */
  async _replace(deadBrowser) {
    const idx = this.browsers.indexOf(deadBrowser);
    if (idx !== -1) this.browsers.splice(idx, 1);
    try {
      const fresh = await this._launch();
      this.browsers.push(fresh);
      this.available.push(fresh);
      logger.warn('Replaced a dead pooled browser');
    } catch (err) {
      logger.error('Failed to relaunch replacement browser:', err.message);
    }
  }

  /**
   * Initialize browser pool
   * Should be called once at worker startup
   */
  async initialize() {
    if (this.isInitialized) {
      return;
    }

    if (this.initPromise) {
      return await this.initPromise;
    }

    this.initPromise = (async () => {
      try {
        logger.info(`🌐 Initializing browser pool (size: ${this.poolSize})...`);

        for (let i = 0; i < this.poolSize; i++) {
          const browser = await this._launch();

          this.browsers.push(browser);
          this.available.push(browser);

          logger.info(`✅ Browser ${i + 1}/${this.poolSize} started`);
        }

        this.isInitialized = true;
        logger.info('✅ Browser pool initialized successfully');
      } catch (err) {
        logger.error('Failed to initialize browser pool:', err);
        this.initPromise = null;
        throw err;
      }
    })();

    return await this.initPromise;
  }

  /**
   * Borrow a browser from the pool
   * Waits if none available, auto-initializes if not yet initialized
   */
  async borrow(timeout = 60000) {
    if (!this.isInitialized) {
      logger.info('Browser pool not initialized on borrow(), initializing on demand...');
      await this.initialize();
    }

    const startTime = Date.now();

    // Wait for a *healthy* available browser
    while (true) {
      if (this.available.length === 0) {
        if (Date.now() - startTime > timeout) {
          throw new Error(`Timeout waiting for available browser (${timeout}ms)`);
        }
        await new Promise(resolve => setTimeout(resolve, 100));
        continue;
      }

      const candidate = this.available.pop();

      // Skip / heal a browser that died while idle (V-15)
      if (typeof candidate.isConnected === 'function' && !candidate.isConnected()) {
        logger.warn('Discarding dead idle browser from pool');
        this._replace(candidate);   // async, fire-and-forget
        continue;
      }

      this.currentlyUsing.add(candidate);
      logger.debug(`Browser borrowed. Available: ${this.available.length}/${this.poolSize}`);
      return candidate;
    }
  }

  /**
   * Return browser to pool. If it died during use, drop it and relaunch a
   * replacement so the pool never silently shrinks (V-15).
   */
  return(browser) {
    if (!this.currentlyUsing.has(browser)) return;
    this.currentlyUsing.delete(browser);

    if (typeof browser.isConnected === 'function' && !browser.isConnected()) {
      logger.warn('Returned browser is dead; relaunching replacement');
      this._replace(browser);       // async, fire-and-forget
      return;
    }

    this.available.push(browser);
    logger.debug(`Browser returned. Available: ${this.available.length}/${this.poolSize}`);
  }

  /**
   * Get current pool stats
   */
  getStats() {
    return {
      poolSize: this.poolSize,
      available: this.available.length,
      inUse: this.currentlyUsing.size,
      total: this.browsers.length
    };
  }

  /**
   * Close all browsers (cleanup)
   */
  async close() {
    try {
      logger.info('🌐 Closing browser pool...');

      for (const browser of this.browsers) {
        try {
          await browser.close();
        } catch (err) {
          logger.warn('Error closing browser:', err.message);
        }
      }

      this.browsers = [];
      this.available = [];
      this.currentlyUsing.clear();
      this.isInitialized = false;
      this.initPromise = null;

      logger.info('✅ Browser pool closed');

    } catch (err) {
      logger.error('Error closing browser pool:', err);
    }
  }

  /**
   * Health check - verify browsers are working
   */
  async healthCheck() {
    try {
      const testBrowser = await this.borrow();
      const page = await testBrowser.newPage();
      await page.goto('about:blank');
      await page.close();
      this.return(testBrowser);

      return { healthy: true };

    } catch (err) {
      logger.error('Browser pool health check failed:', err);
      return { healthy: false, error: err.message };
    }
  }
}

// Create singleton
let browserPoolInstance = null;

export async function getBrowserPool(poolSize = (parseInt(process.env.BROWSER_POOL_SIZE, 10) || 2)) {
  if (!browserPoolInstance) {
    browserPoolInstance = new BrowserPool(poolSize);
  }
  if (!browserPoolInstance.isInitialized) {
    await browserPoolInstance.initialize();
  }
  return browserPoolInstance;
}

export { BrowserPool };