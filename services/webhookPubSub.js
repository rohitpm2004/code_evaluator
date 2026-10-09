import EventEmitter from 'events';
import redisConnection from '../config/redis.js';
import logger from '../config/logger.js';

class WebhookPubSub extends EventEmitter {
  constructor() {
    super();
    this.subscriber = null;
  }

  init() {
    if (this.subscriber) return;
    
    logger.info('Initializing shared Redis subscriber for webhooks...');
    this.subscriber = redisConnection.getClient().duplicate();
    
    // Subscribe to a pattern for all webhooks to avoid 1 connection per job
    this.subscriber.psubscribe('github_webhook_*', (err, count) => {
      if (err) {
        logger.error('Failed to subscribe to github_webhook_* pattern:', err);
      } else {
        logger.info(`Subscribed to github_webhook_* pattern for webhook events`);
      }
    });

    this.subscriber.on('pmessage', (pattern, channel, message) => {
      try {
        logger.info(`WebhookPubSub received pmessage on channel ${channel}`);
        const payload = JSON.parse(message);
        const hasListeners = this.listenerCount(channel) > 0;
        logger.info(`WebhookPubSub emitting to ${channel}. Listeners attached: ${hasListeners}`);
        this.emit(channel, payload);
      } catch (err) {
        logger.error(`Failed to parse webhook message for ${channel}:`, err);
      }
    });
    
    this.subscriber.on('error', (err) => {
      logger.error('Shared webhook subscriber connection error:', err);
    });
  }

  async waitForWebhook(jobId, timeoutMs) {
    this.init(); // ensure subscriber is running

    const redisClient = redisConnection.getClient();
    const cachedKey = `github_webhook_result_${jobId}`;
    const channel = `github_webhook_${jobId}`;

    return new Promise(async (resolve, reject) => {
      let settled = false;
      let timeoutId;

      const cleanup = () => {
        clearTimeout(timeoutId);
        this.off(channel, handler);
      };

      const handler = (payload) => {
        if (settled) return;
        settled = true;
        logger.info(`WebhookPubSub waitForWebhook handler triggered for ${channel}`);
        cleanup();
        redisClient.del(cachedKey).catch(() => {});
        resolve(payload);
      };

      // 1. Attach pub/sub listener FIRST so we close any gap and never miss concurrent deliveries
      logger.info(`WebhookPubSub waitForWebhook setting up listener for ${channel}`);
      this.on(channel, handler);

      timeoutId = setTimeout(() => {
        if (settled) return;
        settled = true;
        cleanup();
        logger.error(`WebhookPubSub waitForWebhook TIMEOUT for ${channel}`);
        reject(new Error("GitHub Actions webhook timeout"));
      }, timeoutMs);

      // 2. Fast path: check Redis for pre-stored webhook result (e.g. arrived before subscription)
      try {
        const cached = await redisClient.get(cachedKey);
        if (cached && !settled) {
          settled = true;
          logger.info(`WebhookPubSub: found pre-stored webhook result for ${jobId} (cache hit — skipping pub/sub wait)`);
          cleanup();
          await redisClient.del(cachedKey).catch(() => {});
          resolve(JSON.parse(cached));
        }
      } catch (err) {
        logger.warn(`WebhookPubSub: Redis key check failed for ${jobId}: ${err.message} — continuing with pub/sub listener`);
      }
    });
  }
}

export const webhookPubSub = new WebhookPubSub();
