import { Queue } from 'bullmq';
import { redisConfig } from '../config/redis.js';
import process from 'process';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const fixturePath = path.resolve(__dirname, '../test/fixtures/excel/golden_01_perfect.xlsx');

console.log('================================================================');
console.log('⚡ EXCEL EVALUATOR LOAD & CONCURRENCY BENCHMARK SUITE');
console.log('================================================================\n');

const queueName = 'excel-evaluation';
const prefix = process.env.QUEUE_PREFIX || 'local';

const queue = new Queue(queueName, {
  connection: redisConfig,
  prefix
});

/**
 * Enqueue a burst of Excel fixture jobs, wait for the queue to drain, and return throughput metrics.
 */
async function runBenchmark(jobCount = 100) {
  console.log(`\n▶ Starting Load Benchmark: ${jobCount} Submissions Burst...`);

  const initialMemory = process.memoryUsage();
  console.log(`Initial Host Memory: RSS ${(initialMemory.rss / 1024 / 1024).toFixed(1)}MB, Heap ${(initialMemory.heapUsed / 1024 / 1024).toFixed(1)}MB`);

  const initialCounts = await queue.getJobCounts('completed', 'failed');

  const startTime = Date.now();
  const jobs = [];

  for (let i = 0; i < jobCount; i++) {
    jobs.push({
      name: `load-test-${i}`,
      data: {
        type: 'excel',
        filePath: fixturePath,
        rubric: {
          criteria: [
            { name: "Structure", weight: 50 },
            { name: "Formulas", weight: 50 }
          ]
        },
        testCases: [
          { criterion: "Structure", type: "format_check", range: "A1:D1", bold: true }
        ],
        submissionId: `bench-${Date.now()}-${i}`,
        skipCache: true
      },
      opts: {
        attempts: 2,
        removeOnComplete: 1000,
        removeOnFail: 1000
      }
    });
  }

  // Bulk add to BullMQ
  console.log(`Enqueuing ${jobCount} jobs to BullMQ (${queueName})...`);
  const enqStart = Date.now();
  await queue.addBulk(jobs);
  const enqDuration = Date.now() - enqStart;
  console.log(`✅ All ${jobCount} jobs enqueued in ${enqDuration}ms (${(jobCount / (enqDuration / 1000)).toFixed(1)} jobs/sec).`);

  // Wait for jobs to drain
  let completed = 0;
  let failed = 0;
  let active = 0;
  let waiting = 0;

  const checkInterval = 1000;
  while (true) {
    const counts = await queue.getJobCounts('waiting', 'active', 'completed', 'failed');
    waiting = counts.waiting;
    active = counts.active;
    completed = Math.max(0, counts.completed - initialCounts.completed);
    failed = Math.max(0, counts.failed - initialCounts.failed);

    const elapsedSec = ((Date.now() - startTime) / 1000).toFixed(1);
    console.log(`[${elapsedSec}s] Waiting: ${waiting} | Active: ${active} | Completed: ${completed} | Failed: ${failed}`);

    if (waiting === 0 && active === 0) {
      break;
    }

    await new Promise(r => setTimeout(r, checkInterval));
  }

  const totalDurationMs = Date.now() - startTime;
  const peakMemory = process.memoryUsage();

  console.log('\n--- Benchmark Results ---');
  console.log(`Total Jobs Processed: ${jobCount}`);
  console.log(`Total Duration: ${(totalDurationMs / 1000).toFixed(2)}s`);
  console.log(`Throughput: ${(jobCount / (totalDurationMs / 1000)).toFixed(2)} jobs/sec`);
  console.log(`Peak RSS: ${(peakMemory.rss / 1024 / 1024).toFixed(1)}MB (Delta: ${((peakMemory.rss - initialMemory.rss) / 1024 / 1024).toFixed(1)}MB)`);
  console.log(`Peak Heap: ${(peakMemory.heapUsed / 1024 / 1024).toFixed(1)}MB`);
  console.log(`Failures: ${failed}`);

  return {
    jobCount,
    totalDurationMs,
    throughput: (jobCount / (totalDurationMs / 1000)),
    failed
  };
}

/**
 * Run the benchmark with the CLI job count and close the queue before exiting.
 */
async function main() {
  try {
    const args = process.argv.slice(2);
    const count = parseInt(args[0], 10) || 100;
    await runBenchmark(count);
  } finally {
    await queue.close();
    process.exit(0);
  }
}

main().catch(err => {
  console.error('Benchmark Error:', err);
  process.exit(1);
});
