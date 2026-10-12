// queues/redis.js
import Redis from 'ioredis';
import dotenv from 'dotenv';
import logger from './logger.js';

dotenv.config();

const redisConfig = {
  host: process.env.REDIS_HOST || '127.0.0.1',
  port: parseInt(process.env.REDIS_PORT) || 6379,
  // V-14: username is a STRING (e.g. "default" for Redis ACL), never parseInt.
  username: process.env.REDIS_USERNAME || undefined,
  password: process.env.REDIS_PASSWORD || undefined,

  connectTimeout: 30000,
  keepAlive: 10000,
  // V-13: no commandTimeout — BullMQ workers issue long blocking commands
  // (BZPOPMIN/BRPOPLPUSH); a command timeout would abort them.
  // Connection options
  retryStrategy: (times) => {
    const delay = Math.min(times * 50, 2000);
    logger.warn(`Redis retry attempt ${times}, delay: ${delay}ms`);
    return delay;
  },
  
  enableReadyCheck: false,
  enableOfflineQueue: true,
  maxRetriesPerRequest: null,
  
  // Performance options
  lazyConnect: false,
};

class RedisConnection {
  constructor() {
    this.client = null;
    this.status = 'disconnected';
  }

  connect() {
    if (this.client) {
      return this.client;
    }

    try {
      this.client = new Redis(redisConfig);

      // Connection events
      this.client.on('connect', () => {
        this.status = 'connected';
        logger.info('Redis connected successfully');
      });

      this.client.on('ready', () => {
        this.status = 'ready';
        logger.info('Redis connection ready');
      });

      this.client.on('error', (err) => {
        this.status = 'error';
        logger.error('Redis connection error:', err);
      });

      this.client.on('reconnecting', () => {
        this.status = 'reconnecting';
        logger.warn('Redis reconnecting...');
      });

      this.client.on('close', () => {
        this.status = 'closed';
        logger.warn('Redis connection closed');
      });

      return this.client;
    } catch (err) {
      logger.error('Failed to create Redis connection:', err);
      throw err;
    }
  }

  disconnect() {
    if (this.client) {
      this.client.disconnect();
      this.client = null;
      this.status = 'disconnected';
      logger.info('Redis connection closed');
    }
  }

  async ping() {
    if (!this.client) {
      this.connect();
    }
    return await this.client.ping();
  }

  async getStatus() {
    return {
      status: this.status,
      info: await this.client?.info('stats')
    };
  }

  // Get the underlying Redis client
  getClient() {
    if (!this.client) {
      this.connect();
    }
    return this.client;
  }

  // Dedicated duplicate client for BullMQ workers to prevent blocking command contention
  getWorkerClient() {
    const main = this.getClient();
    return main.duplicate();
  }
}

const redisConnection = new RedisConnection();

export default redisConnection;
export { RedisConnection, redisConfig };