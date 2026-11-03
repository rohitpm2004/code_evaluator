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

/**
 * Validate single or batch Excel file references, remote URLs, and optional rubric criteria.
 * @throws {ValidationError} When a required file reference or its format is invalid.
 */
async function validateExcelPayload(payload) {
  /**
   * Recognize supported workbook paths and spreadsheet links; reject legacy .xls files.
   */
  const isUrlValidSpreadsheet = (url) => {
    if (!url || typeof url !== 'string') return false;
    const lower = url.trim().toLowerCase();
    if (lower.includes('docs.google.com') || lower.includes('drive.google.com') || lower.includes('sheets.google.com')) return true;
    if (lower.includes('onedrive') || lower.includes('1drv.ms') || lower.includes('sharepoint.com') || lower.includes('api.onedrive.com')) return true;
    if (lower.includes('format=xlsx') || lower.includes('export=download') || lower.includes('/root/content')) return true;
    const clean = lower.split('?')[0];
    if (clean.endsWith('.xls')) {
      throw new ValidationError('Legacy Excel (.xls) format is not supported. Please export or convert your spreadsheet to modern OpenXML (.xlsx).');
    }
    if (clean.endsWith('.xlsx') || clean.endsWith('.xlsm')) return true;
    try {
      const parsed = new URL(url.trim());
      if (parsed.protocol === 'http:' || parsed.protocol === 'https:') return true;
    } catch {}
    return false;
  };

  const urlsToCheck = [];

  if (Array.isArray(payload.submissions) && payload.submissions.length > 0) {
    for (const s of payload.submissions) {
      const u = s.fileUrl || s.submission_file_url || s.filePath || s.submission_link || s.repoUrl;
      if (u && isUrlValidSpreadsheet(u)) {
        urlsToCheck.push(u);
      }
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
      assertUrlSyntax(u);
    }
  }

  if (payload.rubric) {
    validateRubricCriteria(payload.rubric);
  }
}

/**
 * Validate SQL evaluation payload before it enters BullMQ.
 * Checks that submissions provide SQL content (sqlText, ideFiles, or a URL),
 * and that the manifest structure is present with valid test cases.
 * @throws {ValidationError} When required SQL fields are missing or malformed.
 */
export function validateSqlPayload(payload) {
  // Normalize root-level single submission if sent without submissions array
  if (!Array.isArray(payload.submissions) && (payload.submissionId || payload.fileUrl || payload.sqlText)) {
    payload.submissions = [{
      submissionId: payload.submissionId,
      fileUrl: payload.fileUrl || payload.submission_file_url || payload.submission_link,
      sqlText: payload.sqlText,
      ideFiles: payload.ideFiles,
      studentName: payload.studentName,
      studentId: payload.studentId,
    }];
  }

  const { submissions, manifest, testCases } = payload;

  if (!Array.isArray(submissions) || submissions.length === 0) {
    throw new ValidationError('submissions must be a non-empty array for SQL evaluation');
  }

  if (submissions.length > MAX_SUBMISSIONS) {
    throw new ValidationError(`Too many submissions (max ${MAX_SUBMISSIONS})`);
  }

  for (const s of submissions) {
    const hasSqlText = typeof s.sqlText === 'string' && s.sqlText.trim().length > 0;
    const hasIdeFiles = Array.isArray(s.ideFiles) && s.ideFiles.length > 0;
    const hasFileUrl = typeof (s.submission_file_url || s.submission_link || s.repoUrl || s.fileUrl) === 'string';

    if (!hasSqlText && !hasIdeFiles && !hasFileUrl) {
      throw new ValidationError(
        'Each SQL submission must provide sqlText, ideFiles, submission_link, submission_file_url, or repoUrl'
      );
    }

    // If a URL is provided (not inline SQL), do a fast allowlist check
    if (!hasSqlText && !hasIdeFiles) {
      const url = s.submission_file_url || s.submission_link || s.repoUrl || s.fileUrl;
      if (url && (url.startsWith('http://') || url.startsWith('https://'))) {
        // Git repo URLs get allowlist check; S3/presigned URLs are accepted as-is
        try {
          const parsedUrl = new URL(url);
          const gitHosts = getAllowedGitHosts();
          if (gitHosts.some(h => parsedUrl.hostname.endsWith(h))) {
            assertUrlSyntax(url, { allowedHosts: gitHosts });
          }
        } catch (urlErr) {
          if (urlErr instanceof ValidationError) throw urlErr;
          // Non-git URL (S3, presigned) — accepted without git-host check
        }
      }
    }
  }

  // Validate manifest or testCases presence
  const sqlManifest = manifest || testCases;
  if (!sqlManifest || typeof sqlManifest !== 'object') {
    throw new ValidationError(
      'SQL evaluation requires a manifest or testCases object containing fixtures, reference query, and test assertions'
    );
  }

  if (payload.rubric) {
    validateRubricCriteria(payload.rubric);
  }
}

/**
 * Validate an evaluation request, enqueue its submissions, and respond with job IDs or errors.
 */
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
    } else if (payload.type === 'sql') {
      validateSqlPayload(payload);
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
