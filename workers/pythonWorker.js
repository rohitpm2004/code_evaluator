import { Worker } from 'bullmq';
import redisConnection, { redisConfig } from '../config/redis.js';
import queueManager from '../config/queueManager.js';
import logger from '../config/logger.js';
import { triggerGraderWorkflow } from '../services/githubActionService.js';
import { withTimeout } from '../evaluators/react/utils/timeout.js';
import { webhookPubSub } from '../services/webhookPubSub.js';
import { generatePythonAIFeedback } from '../evaluators/python/aiFeedback.js';
import { cloneRepo, deleteRepo } from '../evaluators/react/repoService.js';
import { isDockerAvailable, runPythonInDocker } from '../services/dockerSandboxService.js';

let pythonWorker = null;

export async function initializePythonWorker() {
  try {
    logger.info('Initializing python Worker...');

    const config = queueManager.getConfig('python');

    pythonWorker = new Worker(
      'python-evaluation',
      async (job) => {
          try {
            logger.info(`Starting Python evaluation: ${job.id}`);
            const { submission, testCases, evaluationMode, entryFunction, expectedLogs } = job.data;
            const repoUrl = submission?.repoUrl || submission?.fileUrl || job.data?.fileUrl || job.data?.repoUrl;

            // Phase 0: Check if Docker Sandbox is enabled and available
            const sandboxMode = (process.env.SANDBOX_MODE || "docker").toLowerCase();
            const canUseDocker = sandboxMode === "docker" && (await isDockerAvailable());

            let results;
            // Materialize submission source (direct code, IDE files, or downloaded repo/archive)
            let repoPath = null;
            let didClone = false;
            const directCode = submission?.code || submission?.python_text || job.data?.code;

            if (typeof directCode === 'string' && directCode.trim().length > 0) {
              const tempDir = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'py-eval-'));
              await fs.promises.writeFile(path.join(tempDir, 'main.py'), directCode, 'utf8');
              repoPath = tempDir;
              didClone = true;
            } else if (submission?.ideFiles && Array.isArray(submission.ideFiles) && submission.ideFiles.length > 0) {
              const tempDir = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'py-eval-'));
              for (const file of submission.ideFiles) {
                if (file.path && typeof file.content === 'string') {
                  const fp = path.join(tempDir, file.path);
                  await fs.promises.mkdir(path.dirname(fp), { recursive: true });
                  await fs.promises.writeFile(fp, file.content, 'utf8');
                }
              }
              repoPath = tempDir;
              didClone = true;
            } else {
              repoPath = await cloneRepo(repoUrl);
              didClone = true;
            }

            try {
              results = await runPythonInDocker(repoPath, { testCases, entryFunction, evaluationMode });
            } finally {
              if (didClone && repoPath) {
                await deleteRepo(repoPath);
              }
            }
            
            let finalScore = 0;
            let passedCount = 0;
            let totalCount = 0;

            if (Array.isArray(results)) {
              totalCount = results.length;
              passedCount = results.filter(r => r.passed).length;
              finalScore = totalCount > 0 ? (passedCount / totalCount) * 100 : 0;
            } else if (typeof results?.score === 'number') {
              finalScore = results.score;
              const match = (results.feedback || '').match(/(\d+)\s*of\s*(\d+)/);
              if (match) {
                passedCount = parseInt(match[1], 10);
                totalCount = parseInt(match[2], 10);
              }
            }

            const globalPassRatio = finalScore / 100;

            let rubric = null;
            if (job.data.rubricText && job.data.rubricText !== 'Standard evaluation') {
              try {
                let r = job.data.rubricText;
                while (typeof r === 'string') {
                  r = JSON.parse(r);
                }
                if (Array.isArray(r)) {
                  rubric = { criteria: r };
                } else if (r && r.criteria && Array.isArray(r.criteria)) {
                  rubric = r;
                }
              } catch(e) {
                 logger.warn(`Failed to parse rubricText for Python Job ${job.id}: ${e.message}`);
              }
            }

            const breakdown = [];
            const strengths = [];
            const issues = [];

            if (rubric && rubric.criteria && rubric.criteria.length > 0) {
              finalScore = 0; // Recalculate based on rubric weights
              const testCaseList = Array.isArray(testCases) ? testCases : [];

              for (const c of rubric.criteria) {
                 const maxPts = typeof c.weight === 'number' ? c.weight : (typeof c.score === 'number' ? c.score : 0);
                 const cName = (c.name || '').toLowerCase().trim();

                 // Match test cases bound specifically to this criterion
                 const matchingTests = testCaseList.filter((tc) => {
                   const tcCrit = (tc.criterion || '').toLowerCase().trim();
                   const tcName = (tc.name || '').toLowerCase().trim();
                   return tcCrit === cName || tcName === cName || cName.includes(tcCrit) || tcCrit.includes(cName);
                 });

                 let awardedPts = 0;
                 let reason = '';

                 if (matchingTests.length > 0) {
                   const matchingPassed = matchingTests.filter(tc => {
                     const r = Array.isArray(results) ? (results.find(res => res.name === tc.name) || results[testCaseList.indexOf(tc)]) : null;
                     return r && r.passed;
                   }).length;
                   awardedPts = Math.round((matchingPassed / matchingTests.length) * maxPts);
                   reason = `Passed ${matchingPassed}/${matchingTests.length} tests for this criterion.`;
                 } else {
                   // Fallback to globalPassRatio if tests were not bound by name
                   awardedPts = Math.round(maxPts * globalPassRatio);
                   reason = totalCount > 0 ? `Passed ${passedCount}/${totalCount} total test cases.` : "No test cases run.";
                 }

                 finalScore += awardedPts;

                 breakdown.push({
                   item: c.name,
                   awarded: awardedPts,
                   max: maxPts,
                   reason
                 });

                 if (awardedPts === maxPts) {
                   strengths.push(`[${c.name}] ${reason} (earned ${awardedPts}/${maxPts} marks)`);
                 } else if (awardedPts > 0) {
                   strengths.push(`[${c.name}] Partially correct (${awardedPts}/${maxPts} marks)`);
                   issues.push(`[${c.name}] Missed some test cases: ${reason} (earned ${awardedPts}/${maxPts} marks)`);
                 } else {
                   issues.push(`[${c.name}] ${reason} (earned 0/${maxPts} marks)`);
                 }
              }
            }

            logger.info(`Python Job ${job.id} completed via GitHub Actions. Generating AI feedback...`);
            
            const summaryStr = await generatePythonAIFeedback(job.data, results, finalScore, breakdown, rubric);
            
            const aiFeedbackString = {
              summary: summaryStr,
              strengths: strengths.length > 0 ? strengths : (finalScore === 100 ? ["Passed all tests."] : []),
              issues: issues,
              breakdown: breakdown
            };

            return {
              success: true,
              studentId: submission.studentId,
              studentName: submission.studentName,
              evaluation: {
                score: finalScore,
                feedback: aiFeedbackString,
                details: results
              }
            };
          } catch (err) {
            logger.error(`Python Job ${job.id} failed`, err);
            throw err;
          }
      },
      {
        connection: redisConfig,
        prefix: process.env.QUEUE_PREFIX || 'local',
        concurrency: config.concurrency,
        drainDelay: 5,
        settings: {
          maxStalledCount: 3,
          stalledInterval: 30000,
          lockDuration: 180000,
          lockRenewTime: 30000
        }
      }
    );

    // Event handlers
    pythonWorker.on('completed', (job, result) => {
      logger.info(`Python Job ${job.id} completed`, {
        duration: job.finishedOn - job.processedOn
      });
    });

    pythonWorker.on('failed', (job, err) => {
      logger.error(`Python Job ${job.id} failed`, {
        error: err.message,
        attempts: job.attemptsMade
      });
    });

    pythonWorker.on('error', (err) => {
      logger.error('Python Worker internal error:', err);
    });

    logger.info('Python Worker initialized');
    return pythonWorker;

  } catch (err) {
    logger.error('Failed to initialize Python worker:', err);
    throw err;
  }
}

export async function stopPythonWorker() {
  try {
    if (pythonWorker) {
      await pythonWorker.close();
    }
    logger.info('Python worker stopped');
  } catch (err) {
    logger.error('Error stopping Python worker:', err);
  }
}

export function getPythonWorkerStatus() {
  return {
    status: pythonWorker ? 'running' : 'not_initialized',
    concurrency: queueManager.getConfig('python').concurrency
  };
}

export { pythonWorker };