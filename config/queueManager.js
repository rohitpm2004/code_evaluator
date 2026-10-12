// src/queues/queueManager.js
/**
 * QUEUE MANAGER - Handles all evaluation queues
 * 
 * This integrates with your existing:
 * - evaluationRouter.js
 * - worker files (visualWorker, jsWorker, etc)
 * - Redis connection
 */

import crypto from 'crypto';
import { Queue } from 'bullmq';
import redisConnection, { redisConfig } from '../config/redis.js';
import logger from './logger.js';

/**
 * Queue Configuration
 * 
 * Each evaluator has:
 * - Queue name (matches worker subscription)
 * - Job name (for logging)
 * - Concurrency (how many jobs in parallel)
 * - Timeout (max job duration)
 * - Retries (attempt count)
 */

const QUEUE_CONFIG = {
  react: {
    jobName: 'react-job',
    concurrency: 5,
    timeout: 120000,      // 2 minutes
    description: 'React Component Evaluation'
  },
  
  backend: {
    jobName: 'backend-job',
    concurrency: 3,
    timeout: 180000,      // 3 minutes
    description: 'Backend API Evaluation'
  },
  
  visual: {
    jobName: 'visual-job',
    // Visual is heavy - browsers consume memory. Free-tier deploys run with
    // BROWSER_POOL_SIZE=1, so keep concurrency modest by default.
    concurrency: parseInt(process.env.VISUAL_CONCURRENCY) || 2,
    timeout: 300000,      // 5 minutes (screenshot + browser + AI)
    description: 'Visual/UI Evaluation'
  },
  
  javascript: {
    jobName: 'javascript-job',
    concurrency: parseInt(process.env.JS_CONCURRENCY) || 4,
    timeout: 180000,      // 3 minutes (accounts for Render cold-start ~50s + GitHub Actions ~13s + AI feedback)
    description: 'JavaScript Evaluation'
  },
  
  python: {
    jobName: 'python-job',
    concurrency: 5,
    timeout: 120000,
    description: 'Python Evaluation'
  },
  
  fullstack: {
    jobName: 'fullstack-job',
    concurrency: 2,       // Combines multiple evaluations
    timeout: 300000,      // 5 minutes
    description: 'Full Stack Evaluation'
  }
};

/**
 * Queue Manager Class
 * 
 * Manages all queues, scheduling, and monitoring
 */
class QueueManager {
  constructor() {
    this.queues = {};
    this.isInitialized = false;
  }

  /**
   * Initialize all queues
   * Called once at application startup
   */
  async initialize() {
    if (this.isInitialized) {
      logger.warn('QueueManager already initialized');
      return;
    }

    try {
      logger.info('Initializing Queue Manager...');

      // Create each queue using redisConfig for robust isolated connections
      for (const [type, config] of Object.entries(QUEUE_CONFIG)) {
        this.queues[type] = new Queue(type + '-evaluation', {
          connection: redisConfig,
          prefix: process.env.QUEUE_PREFIX || 'local',
          
          // Default job options
          defaultJobOptions: {
            // Retry failed jobs
            attempts: 3,
            backoff: {
              type: 'exponential',
              delay: 2000  // Start with 2 sec, exponential increase
            },

            // NOTE (V-08): BullMQ v5 removed the per-job `timeout` option — it was
            // a no-op. Real timeouts are now enforced inside the worker via
            // withTimeout(config.timeout).

            // Remove completed jobs after 1 hour
            removeOnComplete: {
              age: 3600
            },
            
            // Keep failed jobs for debugging (remove after 7 days)
            removeOnFail: false
          }
        });

        // Add event listeners
        this.setupQueueListeners(type);

        logger.info(`Queue created: ${type}-evaluation (concurrency: ${config.concurrency})`);
      }

      this.isInitialized = true;
      logger.info('All queues initialized successfully');

    } catch (err) {
      logger.error('Failed to initialize queues:', err);
      throw err;
    }
  }

  /**
   * Setup event listeners for a queue
   */
  setupQueueListeners(type) {
    const queue = this.queues[type];

    // Error handling
    queue.on('error', (err) => {
      logger.error(`Queue error (${type}):`, err);
    });

    // Stalled job (took too long)
    queue.on('stalled', (jobId) => {
      logger.warn(`Job stalled: ${type}/${jobId}`);
    });

    // Job waiting
    queue.on('waiting', (job) => {
      logger.info(`Job waiting: ${type}/${job.id}`);
    });
  }

  /**
   * Add a job to the appropriate queue
   * 
   * Usage:
   * const job = await queueManager.addEvaluation('visual', {...payload});
   */
  async addEvaluation(type, payload) {
    if (!this.queues[type]) {
      throw new Error(`Invalid evaluator type: ${type}`);
    }

    try {
      const config = QUEUE_CONFIG[type];
      const queue = this.queues[type];

      // Derive a unique jobId for every evaluation run.
      // Previously this was a pure content-hash, which caused BullMQ to silently
      // skip re-evaluations of the same student (same hash → same jobId → job
      // already exists → BullMQ returns the old job without queuing a new one →
      // GitHub Action never fires → score stays "hardcoded" from the last run).
      // Adding Date.now() ensures every call produces a fresh job.
      let jobId = payload.jobId;
      if (jobId) {
        if (payload.reEvaluate || payload.isReEvaluation || payload.skipCache) {
          const rand = crypto.randomBytes(4).toString('hex');
          jobId = `${jobId}-${Date.now()}-${rand}`;
        }
      } else {
        const submissionData =
          payload.submission ||
          payload.submissions ||
          payload.repoUrl ||
          payload.submission_link ||
          payload.ideFiles ||
          '';
        const rubricData = payload.rubric || payload.rubricText || '';
        const idPayload = JSON.stringify({
          type,
          submission: submissionData,
          rubric: rubricData,
          testCases: payload.testCases || '',
          expectedUrl: payload.expectedUrl || '',
          assignmentId: payload.assignmentId || '',
        });
        const hash = crypto
          .createHash('sha256')
          .update(idPayload)
          .digest('hex')
          .slice(0, 16);
        // Prefix with timestamp and random entropy to guarantee uniqueness across batch/re-evaluations
        const rand = crypto.randomBytes(4).toString('hex');
        jobId = `${type}-${Date.now()}-${rand}-${hash}`;
      }

      // Add job to queue with deterministic jobId for deduplication
      const jobOptions = {
        jobId: String(jobId),
        removeOnComplete: { count: 100 },
        removeOnFail: { count: 100 }
      };

      // Only assign explicit positive numeric priority if explicitly requested.
      // Omit priority otherwise so BullMQ pushes jobs to standard FIFO wait list
      // without stalling behind prioritized ZSET markers.
      if (typeof payload.priority === 'number' && payload.priority > 0) {
        jobOptions.priority = payload.priority;
      }

      const job = await queue.add(
        config.jobName,
        payload,
        jobOptions
      );

      logger.info(`Job added: ${type}/${job.id}`, {
        evaluator: type,
        jobId: job.id,
        queueLength: (await queue.getJobCounts()).waiting
      });

      return job;

    } catch (err) {
      logger.error(`Failed to add job to ${type} queue:`, err);
      throw err;
    }
  }

  /**
   * Get job status
   */
  async getJobStatus(type, jobId) {
    try {
      const queue = this.queues[type];
      const job = await queue.getJob(jobId);
      if (!job) {
        return null;
      }
      logger.debug(`Job found: ${type}/${jobId}`); // V-33: no full-job/secret dump

      return {
        id: job.id,
        state: await job.getState(),
        progress: job.progress ?? 0,
        data: job.data,
        result: job.returnvalue,
        failedReason: job.failedReason,
        stacktrace: job.stacktrace
      };

    } catch (err) {
      logger.error(`Failed to get job status: ${type}/${jobId}`, err);
      return null;
    }
  }

  /**
   * Remove a job from queue
   */
  async removeJob(type, jobId) {
    try {
      const queue = this.queues[type];
      const job = await queue.getJob(jobId);
      
      if (job) {
        await job.remove();
        logger.info(`Job removed: ${type}/${jobId}`);
        return true;
      }
      
      return false;

    } catch (err) {
      logger.error(`Failed to remove job: ${type}/${jobId}`, err);
      throw err;
    }
  }

  /**
   * Get queue statistics
   */
  async getQueueStats(type) {
    try {
      const queue = this.queues[type];
      const counts = await queue.getJobCounts('waiting', 'active', 'completed', 'failed', 'delayed', 'paused', 'prioritized');

      return {
        evaluator: type,
        config: QUEUE_CONFIG[type],
        stats: {
          waiting: counts.waiting || 0,
          active: counts.active || 0,
          completed: counts.completed || 0,
          failed: counts.failed || 0,
          delayed: counts.delayed || 0,
          paused: counts.paused || 0,
          prioritized: counts.prioritized || 0
        },
        timestamp: new Date().toISOString()
      };

    } catch (err) {
      logger.error(`Failed to get stats for ${type}:`, err);
      throw err;
    }
  }

  /**
   * Get all queues statistics
   */
  async getAllStats() {
    const stats = {};

    for (const type of Object.keys(QUEUE_CONFIG)) {
      stats[type] = await this.getQueueStats(type);
    }

    return stats;
  }

  /**
   * Get queue health status
   */
  async getHealthStatus() {
    try {
      const stats = await this.getAllStats();
      
      // Check for backed up queues
      const backedUp = Object.entries(stats)
        .filter(([_, data]) => data.stats.waiting > 50)
        .map(([type, _]) => type);

      // Check for failed jobs
      const hasFailed = Object.entries(stats)
        .some(([_, data]) => data.stats.failed > 0);

      return {
        status: backedUp.length > 0 ? 'degraded' : 'healthy',
        redisConnected: redisConnection.status === 'ready',
        backedUpQueues: backedUp,
        hasFailed: hasFailed,
        stats: stats
      };

    } catch (err) {
      logger.error('Failed to get health status:', err);
      return { status: 'error', error: err.message };
    }
  }

  /**
   * Clear all jobs from a queue (DANGEROUS - use carefully)
   */
  async clearQueue(type) {
    try {
      const queue = this.queues[type];
      
      // Remove last 1000 jobs
      await queue.clean(0, 1000);
      
      logger.warn(`⚠️  Queue cleared: ${type}`);

    } catch (err) {
      logger.error(`Failed to clear queue ${type}:`, err);
      throw err;
    }
  }

  /**
   * Pause a queue (stop processing new jobs)
   */
  async pauseQueue(type) {
    try {
      const queue = this.queues[type];
      await queue.pause();
      logger.warn(`Queue paused: ${type}`);

    } catch (err) {
      logger.error(`Failed to pause queue ${type}:`, err);
      throw err;
    }
  }

  /**
   * Resume a paused queue
   */
  async resumeQueue(type) {
    try {
      const queue = this.queues[type];
      await queue.resume();
      logger.info(`Queue resumed: ${type}`);

    } catch (err) {
      logger.error(`Failed to resume queue ${type}:`, err);
      throw err;
    }
  }

  /**
   * Disconnect all queues (cleanup)
   */
  async disconnect() {
    try {
      logger.info('Disconnecting all queues...');

      for (const queue of Object.values(this.queues)) {
        await queue.close();
      }

      this.queues = {};
      this.isInitialized = false;

      logger.info('All queues disconnected');

    } catch (err) {
      logger.error('Error disconnecting queues:', err);
    }
  }

  /**
   * Get queue object directly (for workers)
   */
  getQueue(type) {
    if (!this.queues[type]) {
      throw new Error(`Queue not found: ${type}`);
    }
    return this.queues[type];
  }

  /**
   * Get all queue types
   */
  getQueueTypes() {
    return Object.keys(QUEUE_CONFIG);
  }

  /**
   * Get config for a queue type
   */
  getConfig(type) {
    return QUEUE_CONFIG[type];
  }
}

// Create singleton instance
const queueManager = new QueueManager();

export default queueManager;
export { QueueManager, QUEUE_CONFIG };