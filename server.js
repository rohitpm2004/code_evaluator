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

    // 6. Start server
    const PORT = process.env.PORT || 3000;
    app.listen(PORT, () => {
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
let shuttingDown = false;
async function shutdown(signal) {
  if (shuttingDown) return;
  shuttingDown = true;
  logger.info(`Received ${signal}, shutting down gracefully...`);

  const hardExit = setTimeout(() => {
    logger.error('Graceful shutdown timed out — forcing exit');
    process.exit(1);
  }, 30000);
  hardExit.unref();

  try {
    await stopVisualWorker();
    await stopJsWorker();
    await stopBackendWorker();
    await stopReactWorker();
    await stopPythonWorker();
    await stopFullstackWorker();
    await stopExcelWorker();
    await queueManager.disconnect();
    logger.info('Shutdown complete');
    process.exit(0);
  } catch (err) {
    logger.error('Error during shutdown:', err);
    process.exit(1);
  }
}

process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));