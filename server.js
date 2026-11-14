import express from 'express';
import dotenv from 'dotenv';
import path from 'path';
import { fileURLToPath } from 'url';
import { evaluate } from './controller/evaluatorController.js';

import queueManager from './config/queueManager.js';
import logger from './config/logger.js';
import redisConnection from './config/redis.js';

// Sanitize BASE_URL to strip accidental trailing newlines or whitespace
// that can be introduced when copy/pasting into Render's env var UI.
if (process.env.BASE_URL) {
  process.env.BASE_URL = process.env.BASE_URL.trim();
}
// Reload trigger for fullstack evaluator root preservation fix

// Initialize workers
// NOTE: visual + javascript + backend are started for this deploy (backend
// added once backendBugs.md's fixes landed — the worker existed and was
// fully wired to its queue, but was never started here, so
// backend-evaluation jobs would have sat in "waiting" forever regardless of
// how correct the evaluator code was). react/python/fullstack aren't in
// scope yet; re-add their initialize*/stop* calls below to bring them back.
import { initializeVisualWorker, stopVisualWorker } from './workers/visualWorker.js';
import { initializeJsWorker, stopJsWorker } from './workers/jsWorker.js';
import { initializeBackendWorker, stopBackendWorker } from './workers/backendWorker.js';
import { initializeReactWorker, stopReactWorker } from './workers/reactWorker.js';
import { initializePythonWorker, stopPythonWorker } from './workers/pythonWorker.js';
import { initializeFullstackWorker, stopFullstackWorker } from './workers/fullstackWorker.js';
import { initializeExcelWorker, stopExcelWorker } from './workers/excelWorker.js';
import { initializeSqlWorker, stopSqlWorker } from './workers/sqlWorker.js';

import { requireApiKey, evaluateRateLimiter } from './middleware/auth.js';
import webhookRouter from './router/webhookRouter.js';

dotenv.config();

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const app = express();
// V-28: cap request body size to prevent memory-pressure DoS.
app.use(express.json({ limit: process.env.MAX_BODY_SIZE || '2mb' }));

// Serve the web UI (static files in public/). express.static only handles
// GET/HEAD for existing files, so it never shadows POST /evaluate or the
// /jobs and /health routes — they fall through to their handlers.
app.use(express.static(path.join(__dirname, 'public')));

// Startup sequence
/**
 * Connect Redis, initialize queues and workers, register API routes, and start the HTTP server.
 */
async function startServer() {
  try {
    logger.info('🚀 Starting evaluator system...');

    // 1. Connect Redis
    await redisConnection.ping();
    logger.info('✅ Redis connected');

    // 2. Initialize queue manager
    await queueManager.initialize();
    logger.info('✅ Queue manager initialized');

    // 3. Initialize workers
    await initializeVisualWorker();
    logger.info('✅ Visual worker initialized');

    await initializeJsWorker();
    logger.info('✅ JavaScript worker initialized');

    await initializeBackendWorker();
    logger.info('✅ Backend worker initialized');

    await initializeReactWorker();
    logger.info('✅ React worker initialized');

    await initializePythonWorker();
    logger.info('✅ Python worker initialized');

    await initializeFullstackWorker();
    logger.info('✅ Fullstack worker initialized');

    await initializeExcelWorker();
    logger.info('✅ Excel worker initialized');

    await initializeSqlWorker();
    logger.info('✅ SQL worker initialized');

    // 4. API routes (V-04: rate limit + API-key auth)
    app.post('/evaluate', evaluateRateLimiter, requireApiKey, evaluate);
    app.use('/api/webhook', webhookRouter);



    // 5. Health check endpoints
    app.get('/health', (req, res) => {
      res.json({
        status: 'OK',
        timestamp: new Date().toISOString(),
        uptime: process.uptime()
      });
    });

    app.get('/health/redis', async (req, res) => {
      try {
        await redisConnection.ping();
        res.json({ redis: 'connected' });
      } catch (err) {
        res.status(500).json({ redis: 'disconnected', error: err.message });
      }
    });

    app.get('/health/redis-memory', async (req, res) => {
      try {
        const conn = redisConnection.connect();
        const memInfo = await conn.info('memory');
        const usedMatch = memInfo.match(/used_memory_human:([^\r\n]+)/);
        const maxMatch = memInfo.match(/maxmemory_human:([^\r\n]+)/);
        const peakMatch = memInfo.match(/used_memory_peak_human:([^\r\n]+)/);
        res.json({
          used_memory: usedMatch ? usedMatch[1] : 'unknown',
          max_memory: maxMatch ? maxMatch[1] : 'unlimited',
          peak_memory: peakMatch ? peakMatch[1] : 'unknown'
        });
      } catch (err) {
        res.status(500).json({ error: err.message });
      }
    });

    app.all('/admin/redis-flush', requireApiKey, async (req, res) => {
      try {
        const conn = redisConnection.connect();
        const prefix = process.env.QUEUE_PREFIX || 'local';
        const keys = await conn.keys(`${prefix}:*`);
        let unlinked = 0;
        if (keys.length > 0) {
          unlinked = await conn.unlink(...keys);
        }
        res.json({
          success: true,
          message: `Flushed ${unlinked} Redis queue keys`,
          keysFound: keys.length
        });
      } catch (err) {
        res.status(500).json({ success: false, error: err.message });
      }
    });

    app.get('/health/queues', async (req, res) => {
      try {
        const stats = await queueManager.getAllStats();
        res.json(stats);
      } catch (err) {
        res.status(500).json({ error: err.message });
      }
    });

    app.get('/jobs/:type/:jobId', requireApiKey, async (req, res) => {
      try {
        const result = await queueManager.getJobStatus(
          req.params.type,
          req.params.jobId
        );

        if (!result) {
          return res.status(404).json({
            success: false,
            error: 'Job not found'
          });
        }

        // Sanitize sensitive internal details if needed
        const sanitized = {
          id: result.id,
          state: result.state,
          progress: result.progress,
          result: result.result,
          failedReason: result.failedReason,
          data: result.data
        };

        res.json(sanitized);
      } catch (err) {
        logger.error(`Error retrieving job status: ${err.message}`);
        res.status(500).json({
          success: false,
          error: err.message
        });
      }
    });

    app.post('/jobs/batch-status', requireApiKey, async (req, res) => {
      try {
        const { type, jobIds } = req.body || {};
        if (!Array.isArray(jobIds) || jobIds.length === 0) {
          return res.status(400).json({ success: false, error: 'jobIds must be a non-empty array' });
        }
        const statuses = await queueManager.getBatchJobStatus(type, jobIds);
        res.json({ success: true, statuses });
      } catch (err) {
        logger.error(`Error retrieving batch job status: ${err.message}`);
        res.status(500).json({ success: false, error: err.message });
      }
    });

    // 6. Start server (bind explicitly to 0.0.0.0 for dual IPv4/loopback clarity on Windows)
    const PORT = process.env.PORT || 3000;
    httpServer = app.listen(PORT, '0.0.0.0', () => {
      logger.info(`API Gateway running on port ${PORT}`);
    });

  } catch (err) {
    logger.error('Failed to start server:', err);
    process.exit(1);
  }
}

startServer();

// V-41: last-resort safety net. A single evaluator's stray rejection must never
// take the whole gateway down. Log it; keep serving. (Workers already await and
// mark their own jobs failed — this only catches truly escaped rejections.)
process.on('unhandledRejection', (reason) => {
  logger.error('Unhandled promise rejection (contained, not crashing):', reason);
});
process.on('uncaughtException', (err) => {
  // A synchronous programmer error — log and let the process manager restart us.
  logger.error('Uncaught exception — shutting down:', err);
  shutdown('uncaughtException');
});

// Graceful shutdown (V-27)
// Drain workers (waits for in-flight jobs + closes their browser pools),
// then close queues/Redis. A hard-exit timer guards against a hung close
// so we never leak zombie Chromium processes across a deploy.
let httpServer = null;
let shuttingDown = false;
/**
 * Drain workers and disconnect queues on shutdown, with a deadline that forces process exit.
 */
async function shutdown(signal) {
  if (shuttingDown) return;
  shuttingDown = true;
  logger.info(`Received ${signal}, shutting down gracefully...`);

  // Force exit within 8s if any connection or socket hangs
  const hardExit = setTimeout(() => {
    logger.error('Graceful shutdown timed out — forcing exit');
    process.exit(1);
  }, 8000);

  try {
    if (httpServer) {
      await new Promise((resolve) => httpServer.close(resolve)).catch(() => {});
    }
    await stopVisualWorker();
    await stopJsWorker();
    await stopBackendWorker();
    await stopReactWorker();
    await stopPythonWorker();
    await stopFullstackWorker();
    await stopExcelWorker();
    await stopSqlWorker();
    await queueManager.disconnect().catch(() => {});
    logger.info('Shutdown complete');
    clearTimeout(hardExit);
    process.exit(0);
  } catch (err) {
    logger.error('Error during shutdown:', err);
    process.exit(1);
  }
}

process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));