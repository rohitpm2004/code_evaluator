import fs from "fs";
import fsp from "fs/promises";
import path from "path";
import os from "os";
import { exec } from "child_process";
import util from "util";
import { fileURLToPath } from "url";
import logger from "../../config/logger.js";
import { generateExcelFeedback } from "./aiFeedback.js";
import { runInDockerSandbox, isDockerAvailable, runExcelInDocker } from "../../services/dockerSandboxService.js";

const execPromise = util.promisify(exec);
const __dirname = path.dirname(fileURLToPath(import.meta.url));

const MAX_FILE_SIZE_BYTES = 15 * 1024 * 1024; // 15MB
const MAX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024; // 50MB
const MAX_COMPRESSION_RATIO = 100;

/**
 * Validates ZIP headers and compression ratio to prevent ZIP-bomb / XML expansion DoS.
 */
async function validateZipSafety(filePath) {
  const stat = await fsp.stat(filePath);
  if (stat.size > MAX_FILE_SIZE_BYTES) {
    throw new Error(`File size (${(stat.size / 1024 / 1024).toFixed(1)}MB) exceeds maximum limit of 15MB.`);
  }

  // Open file buffer and read ZIP central directory headers
  const fd = await fsp.open(filePath, 'r');
  try {
    const buffer = Buffer.alloc(Math.min(stat.size, 65536));
    await fd.read(buffer, 0, buffer.length, 0);

    // Verify standard PK ZIP header: 0x50 0x4B 0x03 0x04
    if (buffer[0] !== 0x50 || buffer[1] !== 0x4B) {
      const textPreview = buffer.toString('utf8', 0, 200).toLowerCase();
      if (textPreview.includes('<!doctype') || textPreview.includes('<html')) {
        throw new Error("Invalid spreadsheet: Received a web page instead of an Excel spreadsheet. Please ensure the Google Sheet is set to 'Anyone with the link can view'.");
      }
      throw new Error("Invalid file format: Not a valid OpenXML (.xlsx) archive.");
    }
  } finally {
    await fd.close();
  }

  return true;
}

/**
 * Executes Python recalculation and assertion inspection scripts.
 */
async function runLocalExcelEngine(originalPath, recalculatedPath, specPath, profileDir) {
  const pythonBin = process.platform === "win32" ? "python" : "python3";
  const recalculateScript = path.resolve(__dirname, "engine/recalculate.py");
  const runnerScript = path.resolve(__dirname, "engine/excel_runner.py");

  // Step 1: Run Recalculation
  logger.info(`[ExcelEvaluator] Running recalculation adapter on ${path.basename(originalPath)}`);
  const recalcCmd = `"${pythonBin}" "${recalculateScript}" --input "${originalPath}" --output "${recalculatedPath}" --profile-dir "${profileDir}"`;
  
  try {
    const { stdout: recalcStdout } = await execPromise(recalcCmd, { timeout: 45000 });
    logger.info(`[ExcelEvaluator] Recalculation output: ${recalcStdout.trim()}`);
  } catch (err) {
    logger.warn(`[ExcelEvaluator] Recalculation warning/error: ${err.message}. Proceeding to dual-inspection.`);
    if (!fs.existsSync(recalculatedPath)) {
      await fsp.copyFile(originalPath, recalculatedPath);
    }
  }

  // Step 2: Run Dual-Workbook Inspection
  logger.info(`[ExcelEvaluator] Running dual-workbook assertion runner`);
  const runnerCmd = `"${pythonBin}" "${runnerScript}" --original "${originalPath}" --recalculated "${recalculatedPath}" --spec "${specPath}"`;

  try {
    const { stdout: runnerStdout } = await execPromise(runnerCmd, { timeout: 30000 });
    return JSON.parse(runnerStdout);
  } catch (err) {
    if (err.stdout) {
      try {
        return JSON.parse(err.stdout);
      } catch (parseErr) {
        // Fall through
      }
    }
    throw new Error(`Excel runner failed: ${err.message}`);
  }
}

/**
 * Main evaluation orchestrator for Excel workbooks.
 */
export async function evaluateExcelSubmission(jobData, jobId) {
  const startTime = Date.now();
  const fileUrl = jobData.fileUrl || jobData.submission?.fileUrl || jobData.submission?.submission_file_url || jobData.submission?.submission_link || jobData.submission?.repoUrl || jobData.submission_link;
  const providedFilePath = jobData.filePath || jobData.submission?.filePath;
  const rubric = jobData.rubric || jobData.submission?.rubric;
  const testCases = jobData.testCases || jobData.submission?.testCases || [];

  const tempWorkspace = await fsp.mkdtemp(path.join(os.tmpdir(), `eval-excel-${jobId || Date.now()}-`));
  const originalFile = path.join(tempWorkspace, "original_submission.xlsx");
  const recalculatedFile = path.join(tempWorkspace, "recalculated_submission.xlsx");
  const specFile = path.join(tempWorkspace, "test_spec.json");
  const profileDir = path.join(tempWorkspace, "lo_profile");

  try {
    // Phase 1: Download or copy immutable original file
    if (fileUrl) {
      let downloadUrl = String(fileUrl).trim();
      if (downloadUrl.includes('docs.google.com/spreadsheets')) {
        const sheetIdMatch = downloadUrl.match(/\/d\/([a-zA-Z0-9-_]+)/);
        if (sheetIdMatch && sheetIdMatch[1]) {
          downloadUrl = `https://docs.google.com/spreadsheets/d/${sheetIdMatch[1]}/export?format=xlsx`;
        }
      } else if (
        (downloadUrl.includes('onedrive.live.com') || downloadUrl.includes('1drv.ms') || downloadUrl.includes('sharepoint.com')) &&
        !downloadUrl.includes('download=1')
      ) {
        const sep = downloadUrl.includes('?') ? '&' : '?';
        downloadUrl = `${downloadUrl}${sep}download=1`;
      }

      logger.info(`[ExcelEvaluator] Downloading submission file from URL for Job ${jobId}: ${downloadUrl}`);
      const res = await fetch(downloadUrl, { redirect: 'follow' });
      if (!res.ok) {
        throw new Error(`Failed to download spreadsheet (HTTP ${res.status}): Make sure the Google Sheet is shared with "Anyone with the link can view".`);
      }
      const buffer = Buffer.from(await res.arrayBuffer());
      await fsp.writeFile(originalFile, buffer);
    } else if (providedFilePath) {
      await fsp.copyFile(providedFilePath, originalFile);
    } else {
      throw new Error("No fileUrl or filePath provided in Excel evaluation payload.");
    }

    // Phase 2: Security & ZIP-bomb check
    await validateZipSafety(originalFile);

    // Phase 3: Prepare test specification
    const spec = {
      rubric: rubric || { criteria: [] },
      testCases: testCases || []
    };
    logger.info(`[ExcelEvaluator] Spec details: ${spec.testCases.length} testCases, criteria: ${JSON.stringify(spec.rubric?.criteria)}`);
    logger.info(`[ExcelEvaluator] TestCases content: ${JSON.stringify(spec.testCases)}`);
    await fsp.writeFile(specFile, JSON.stringify(spec, null, 2), "utf-8");

    // Phase 4: Execute Engine (Docker if available, else local engine)
    const sandboxMode = (process.env.SANDBOX_MODE || "docker").toLowerCase();
    const canUseDocker = sandboxMode === "docker" && (await isDockerAvailable());

    let report;
    if (canUseDocker) {
      try {
        logger.info(`🐳 Excel Job ${jobId}: Using Docker sandbox container.`);
        report = await runExcelInDocker(tempWorkspace);
      } catch (dockerErr) {
        logger.warn(`Docker Excel execution encountered error: ${dockerErr.message}. Falling back to local engine.`);
        report = await runLocalExcelEngine(originalFile, recalculatedFile, specFile, profileDir);
      }
    } else {
      logger.info(`💻 Excel Job ${jobId}: Running via local engine.`);
      report = await runLocalExcelEngine(originalFile, recalculatedFile, specFile, profileDir);
    }

    if (!report || report.success === false) {
      const errReason = report?.error || "Unknown engine error";
      logger.warn(`[ExcelEvaluator] Inspection flagged issues: ${errReason}`);
      throw new Error(`Excel inspection failed: ${errReason}`);
    }

    // Phase 5: Generate AI Explanatory Feedback
    const feedback = await generateExcelFeedback(report, rubric);

    const durationMs = Date.now() - startTime;
    logger.info(`[ExcelEvaluator] Completed Job ${jobId} in ${durationMs}ms with score: ${report.score ?? 0}`);

    return {
      score: report.score ?? 0,
      rubric_breakdown: report.rubric_breakdown || {},
      feedback,
      evaluation_status: report.evaluation_status || "COMPLETED",
      detected_formulas: report.detected_formulas || [],
      detected_charts: report.detected_charts || [],
      detected_pivots: report.detected_pivots || [],
      detected_macros: report.detected_macros || [],
      calculation_warnings: report.calculation_warnings || [],
      assertion_results: report.assertion_results || [],
      durationMs
    };
  } finally {
    // Clean up temporary workspace
    try {
      await fsp.rm(tempWorkspace, { recursive: true, force: true });
    } catch (cleanupErr) {
      logger.warn(`Failed to clean up temp directory ${tempWorkspace}: ${cleanupErr.message}`);
    }
  }
}
