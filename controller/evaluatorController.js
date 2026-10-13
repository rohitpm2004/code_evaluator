// src/controllers/evaluationController.js

import { routeEvaluation } from '../router/evaluationRouter.js';
import logger from '../config/logger.js';
import {
  assertSafeUrl,
  assertUrlSyntax,
  getAllowedGitHosts
} from '../evaluators/visual/utils/urlGuard.js';

const MAX_SUBMISSIONS = Number(process.env.MAX_SUBMISSIONS) || 500;
const MAX_RUBRIC_CHARS = Number(process.env.MAX_RUBRIC_CHARS) || 20000;

export class ValidationError extends Error {
  constructor(message) {
    super(message);
    this.name = 'ValidationError';
  }
}

// Validate + sanitize the visual payload (V-28) and its URLs (V-03).
async function validateVisualPayload(payload) {
  const { submissions, expectedUrl, rubricText } = payload;

  if (!Array.isArray(submissions) || submissions.length === 0) {
    throw new ValidationError('submissions must be a non-empty array');
  }
  if (submissions.length > MAX_SUBMISSIONS) {
    throw new ValidationError(`Too many submissions (max ${MAX_SUBMISSIONS})`);
  }
  if (typeof rubricText !== 'string' || !rubricText.trim()) {
    throw new ValidationError('rubricText is required');
  }
  if (rubricText.length > MAX_RUBRIC_CHARS) {
    throw new ValidationError(`rubricText too large (max ${MAX_RUBRIC_CHARS} chars)`);
  }
  if (typeof expectedUrl !== 'string' && !payload.ideFiles) {
    throw new ValidationError('expectedUrl is required');
  }

  // Full SSRF check on the single reference URL if it's a URL
  if (expectedUrl && expectedUrl.startsWith('http')) {
    await assertSafeUrl(expectedUrl);
  }

  // Fast syntactic + allowlist check on each repo URL (full DNS check runs at clone).
  const allowedHosts = getAllowedGitHosts();
  for (const s of submissions) {
    if (s.ideFiles) {
      if (!Array.isArray(s.ideFiles) || s.ideFiles.length === 0) {
        throw new ValidationError('ideFiles must be a non-empty array');
      }
    } else {
      if (!s || typeof s.repoUrl !== 'string') {
        throw new ValidationError('each submission needs a repoUrl');
      }
      assertUrlSyntax(s.repoUrl, { allowedHosts });
    }
  }
}

// Author: Arma Sahar (backendBugs.md #7)
// Backend submissions used to reach `extractSubmission()` (inside the
// worker, after the job was already queued and picked up) with zero
// validation — a malformed or malicious payload burned a full BullMQ
// attempt budget (3 retries, per queueManager.js) before failing, and the
// caller never got a clean 400. `visual` payloads already fail fast here,
// synchronously, before anything is queued; this brings `backend` in line
// with that. Only a syntax + allowlist check runs here (cheap, no DNS) —
// the full SSRF check (assertSafeUrl, with DNS resolution) still runs
// again right before the actual clone, same as the visual evaluator does.
function validateRubricCriteria(rubric) {
  if (!rubric || !Array.isArray(rubric.criteria) || rubric.criteria.length === 0) {
    throw new ValidationError('rubric.criteria must be a non-empty array');
  }

  // Bug found during audit, confirmed by actually running evaluateResults():
  // a criterion missing/with a non-numeric `weight` doesn't just score that
  // one criterion wrong — `maxScore += possiblePoints` accumulates a
  // running total, so one bad criterion turns the *entire* score NaN,
  // silently, for every submission graded against that rubric. Reject it
  // here instead of scoring every future submission as NaN.
  rubric.criteria.forEach((criterion, i) => {
    if (!criterion || typeof criterion.name !== 'string' || !criterion.name.trim()) {
      throw new ValidationError(`rubric.criteria[${i}] is missing a string "name".`);
    }
    if (typeof criterion.weight !== 'number' || !Number.isFinite(criterion.weight) || criterion.weight <= 0) {
      throw new ValidationError(`rubric.criteria[${i}] ("${criterion.name}") must have a positive numeric "weight".`);
    }
  });
}

function validateBackendPayload(payload) {
  const { repoUrl, ideFiles, rubric } = payload;

  if (ideFiles) {
    if (!Array.isArray(ideFiles) || ideFiles.length === 0) {
      throw new ValidationError('ideFiles must be a non-empty array');
    }
  } else {
    if (typeof repoUrl !== 'string' || !repoUrl.trim()) {
      throw new ValidationError('repoUrl is required');
    }
    assertUrlSyntax(repoUrl, { allowedHosts: getAllowedGitHosts() });
  }

  validateRubricCriteria(rubric);
}

function validatePythonPayload(payload) {
  const { submissions } = payload;
  if (!Array.isArray(submissions) || submissions.length === 0) {
    throw new ValidationError('submissions must be a non-empty array');
  }
  const allowedHosts = getAllowedGitHosts();
  for (const s of submissions) {
    if (!s || typeof s.repoUrl !== 'string') {
      throw new ValidationError('each submission needs a repoUrl');
    }
    assertUrlSyntax(s.repoUrl, { allowedHosts });
  }
}

// react. repoUrl + rubric. same criteria shape as backend.
function validateReactPayload(payload) {
  const { repoUrl, ideFiles, rubric } = payload;
  
  if (ideFiles) {
    if (!Array.isArray(ideFiles) || ideFiles.length === 0) {
      throw new ValidationError('ideFiles must be a non-empty array');
    }
  } else {
    if (typeof repoUrl !== 'string' || !repoUrl.trim()) {
      throw new ValidationError('repoUrl is required');
    }
    assertUrlSyntax(repoUrl, { allowedHosts: getAllowedGitHosts() });
  }
  
  validateRubricCriteria(rubric);
}

function validateJavascriptPayload(payload) {
  const { submissions, repoUrl } = payload;
  if (Array.isArray(submissions)) {
    if (submissions.length === 0) {
      throw new ValidationError('submissions must be a non-empty array');
    }
    const allowedHosts = getAllowedGitHosts();
    for (const s of submissions) {
      if (!s || typeof s.repoUrl !== 'string' || !s.repoUrl.trim()) {
        throw new ValidationError('each submission needs a valid repoUrl');
      }
      assertUrlSyntax(s.repoUrl, { allowedHosts });
    }
  } else if (typeof repoUrl === 'string' && repoUrl.trim()) {
    assertUrlSyntax(repoUrl, { allowedHosts: getAllowedGitHosts() });
  } else {
    throw new ValidationError('submissions (non-empty array) or repoUrl is required for javascript evaluation');
  }
}

function validateFullstackPayload(payload) {
  const { repoUrl, submission_link, rubric } = payload;
  const targetUrl = repoUrl || submission_link;
  if (typeof targetUrl !== 'string' || !targetUrl.trim()) {
    throw new ValidationError('repoUrl or submission_link is required for fullstack evaluation');
  }
  assertUrlSyntax(targetUrl, { allowedHosts: getAllowedGitHosts() });
  validateRubricCriteria(rubric);
}

async function validateExcelPayload(payload) {
  const isUrlValidSpreadsheet = (url) => {
    if (!url || typeof url !== 'string') return false;
    const lower = url.trim().toLowerCase();
    if (lower.includes('docs.google.com/spreadsheets') || lower.includes('sheets.google.com')) return true;
    if (lower.includes('onedrive') || lower.includes('1drv.ms') || lower.includes('sharepoint.com')) return true;
    if (lower.includes('format=xlsx')) return true;
    const clean = lower.split('?')[0];
    if (clean.endsWith('.xlsx') || clean.endsWith('.xls') || clean.endsWith('.xlsm')) return true;
    return false;
  };

  const urlsToCheck = [];

  if (Array.isArray(payload.submissions) && payload.submissions.length > 0) {
    for (const s of payload.submissions) {
      const u = s.fileUrl || s.submission_file_url || s.filePath || s.submission_link || s.repoUrl;
      if (!u || !isUrlValidSpreadsheet(u)) {
        throw new ValidationError(`Invalid file type in batch: Each submission must be a valid .xlsx file or Google Sheets / Excel link. Received: "${u || 'none'}"`);
      }
      urlsToCheck.push(u);
    }
  } else {
    const fileUrl = payload.fileUrl || payload.submission_file_url || payload.filePath || payload.submission_link || payload.repoUrl;
    if (!fileUrl || typeof fileUrl !== 'string' || !fileUrl.trim()) {
      throw new ValidationError('fileUrl or filePath is required for excel evaluation');
    }
    if (!isUrlValidSpreadsheet(fileUrl)) {
      throw new ValidationError('Invalid file type: Excel evaluator accepts .xlsx workbooks and Google Sheets / Excel links.');
    }
    urlsToCheck.push(fileUrl);
  }

  for (const u of urlsToCheck) {
    if (u.startsWith('http://') || u.startsWith('https://')) {
      await assertSafeUrl(u);
    }
  }

  if (payload.rubric) {
    validateRubricCriteria(payload.rubric);
  }
}

export async function evaluate(req, res) {
  try {
    const payload = req.body || {};

    if (!payload.type) {
      return res.status(400).json({
        success: false,
        error: 'Missing required field: type'
      });
    }

    if (payload.type === 'visual') {
      await validateVisualPayload(payload);
    } else if (payload.type === 'backend') {
      validateBackendPayload(payload);
    } else if (payload.type === 'python') {
      validatePythonPayload(payload);
    } else if (payload.type === 'react') {
      validateReactPayload(payload);
    } else if (payload.type === 'javascript') {
      validateJavascriptPayload(payload);
    } else if (payload.type === 'fullstack') {
      validateFullstackPayload(payload);
    } else if (payload.type === 'excel') {
      await validateExcelPayload(payload);
    }

    const jobs = await routeEvaluation(payload);

    logger.info('Evaluation job created', { type: payload.type });

    if (Array.isArray(jobs)) {
      return res.json({
        success: true,
        jobs: jobs.map(job => ({
          jobId: job.id,
          statusUrl: `/jobs/${payload.type}/${job.id}`
        }))
      });
    }

    return res.json({
      success: true,
      jobId: jobs.id,
      statusUrl: `/jobs/${payload.type}/${jobs.id}`
    });

  } catch (err) {
    // Bad input → 400; everything else → 500.
    const isBadInput =
      err instanceof ValidationError ||
      err.name === 'ValidationError' ||
      /Invalid evaluator type|Invalid URL|Disallowed URL scheme|Host not in allowlist|private\/loopback|DNS resolution failed/.test(
        err.message
      );

    if (isBadInput) {
      return res.status(400).json({ success: false, error: err.message });
    }

    logger.error('Evaluation error:', err);
    return res.status(500).json({ success: false, error: err.message });
  }
}
