module.exports = {
  apps: [
    {
      name: 'central-evaluator',
      script: './server.js',
      instances: 1, // Single gateway instance with BullMQ concurrency
      autorestart: true,
      watch: false,
      max_memory_restart: '1500M',
      env_production: {
        NODE_ENV: 'production',
        PORT: 3000
      },
      time: true,
      error_file: './logs/pm2-error.log',
      out_file: './logs/pm2-out.log',
      merge_logs: true
    }
  ]
};
