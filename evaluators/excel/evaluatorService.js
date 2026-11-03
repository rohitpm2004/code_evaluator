import fs from "fs";
import fsp from "fs/promises";
import path from "path";
import os from "os";
import { exec } from "child_process";
import util from "util";
import { Readable } from "stream";
import { fileURLToPath } from "url";
import logger from "../../config/logger.js";
import { generateExcelFeedback } from "./aiFeedback.js";
import { runInDockerSandbox, isDockerAvailable, runExcelInDocker } from "../../services/dockerSandboxService.js";
import { assertSafeUrl } from "../visual/utils/urlGuard.js";

const execPromise = util.promisify(exec);
const __dirname = path.dirname(fileURLToPath(import.meta.url));

const MAX_FILE_SIZE_BYTES = 15 * 1024 * 1024; // 15MB
const MAX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024; // 50MB
const MAX_COMPRESSION_RATIO = 100;
const MAX_REDIRECT_HOPS = 8;

/**
 * Downloads a spreadsheet safely with:
 * 1. Manual redirect following with SSRF re-validation at each hop (max 8 redirects).
 * 2. Streaming response directly to disk to prevent unbounded heap memory buffering.
 * 3. Enforcing hard byte limit (MAX_FILE_SIZE_BYTES = 15MB).
 */
async function safeDownloadSpreadsheet(targetUrl, destPath, maxBytes = MAX_FILE_SIZE_BYTES) {
  let currentUrl = targetUrl;
  let res = null;

  for (let hop = 0; hop <= MAX_REDIRECT_HOPS; hop++) {
    await assertSafeUrl(currentUrl);

    res = await fetch(currentUrl, {
      redirect: "manual",
      headers: {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/vnd.ms-excel,text/csv,*/*",
        "Accept-Language": "en-US,en;q=0.9"
      }
    });

    // Check for redirect status codes: 301, 302, 303, 307, 308
    if ([301, 302, 303, 307, 308].includes(res.status)) {
      const location = res.headers.get("location");
      if (!location) {
        throw new Error(`Received HTTP ${res.status} redirect without Location header.`);
      }
      currentUrl = new URL(location, currentUrl).href;
      continue;
    }

    break;
  }

  if (!res || !res.ok) {
    const status = res ? res.status : "Unknown";
    const isGoogle = currentUrl.includes("google.com") || targetUrl.includes("google.com");
    const isOneDrive =
      currentUrl.includes("1drv.ms") ||
      currentUrl.includes("onedrive.live.com") ||
      currentUrl.includes("sharepoint.com") ||
      currentUrl.includes("api.onedrive.com") ||
      targetUrl.includes("1drv.ms") ||
      targetUrl.includes("onedrive.live.com") ||
      targetUrl.includes("sharepoint.com") ||
      targetUrl.includes("api.onedrive.com");

    let helpMsg = 'Make sure the file or spreadsheet is shared with "Anyone with the link can view".';
    if (isGoogle) {
      helpMsg = 'Make sure the Google Sheet or Drive file is shared with "Anyone with the link can view".';
    } else if (isOneDrive) {
      helpMsg = 'OneDrive / Excel Online links require Microsoft authentication and block automated server downloads. Please have the student upload the .xlsx file directly or share via Google Sheets with "Anyone with the link can view".';
    }
    throw new Error(`Failed to download spreadsheet (HTTP ${status}): ${helpMsg}`);
  }

  // Check Content-Length header if provided
  const contentLength = res.headers.get("content-length");
  if (contentLength && parseInt(contentLength, 10) > maxBytes) {
    throw new Error(`File size (${(parseInt(contentLength, 10) / 1024 / 1024).toFixed(1)}MB) exceeds maximum limit of 15MB.`);
  }

  // Stream directly to disk and count bytes to prevent unbounded heap allocation
  const writeStream = fs.createWriteStream(destPath);
  let downloadedBytes = 0;

  try {
    const readable = Readable.fromWeb(res.body);
    for await (const chunk of readable) {
      downloadedBytes += chunk.length;
      if (downloadedBytes > maxBytes) {
        throw new Error(`File size (${(downloadedBytes / 1024 / 1024).toFixed(1)}MB) exceeds maximum limit of 15MB.`);
      }
      if (!writeStream.write(chunk)) {
        await new Promise((resolve) => writeStream.once("drain", resolve));
      }
    }
  } catch (err) {
    writeStream.destroy();
    try { await fsp.unlink(destPath); } catch {}
    throw err;
  } finally {
    writeStream.end();
    await new Promise((resolve) => writeStream.once("finish", resolve));
  }
}

/**
 * Validates ZIP headers, uncompressed size, and compression ratio to prevent ZIP-bomb / XML expansion DoS.
 */
async function validateZipSafety(filePath) {
  const stat = await fsp.stat(filePath);
  if (stat.size > MAX_FILE_SIZE_BYTES) {
    throw new Error(`File size (${(stat.size / 1024 / 1024).toFixed(1)}MB) exceeds maximum limit of 15MB.`);
  }
  if (stat.size < 22) {
    throw new Error("Invalid file format: File too small to be a valid ZIP archive.");
  }

  const fd = await fsp.open(filePath, "r");
  try {
    // 1. Verify standard PK ZIP header: 0x50 0x4B
    const headBuf = Buffer.alloc(Math.min(stat.size, 1024));
    await fd.read(headBuf, 0, headBuf.length, 0);

    if (headBuf[0] !== 0x50 || headBuf[1] !== 0x4B) {
      const textPreview = headBuf.toString("utf8", 0, 500).toLowerCase();
      if (textPreview.includes("<!doctype") || textPreview.includes("<html") || textPreview.includes("<head") || textPreview.includes("<body")) {
        throw new Error(
          "Invalid spreadsheet: Received an HTML web page instead of an Excel spreadsheet (.xlsx). If using Google Sheets or OneDrive, verify that sharing is set to 'Anyone with the link can view' without requiring sign-in, or submit directly as an uploaded Excel file (.xlsx)."
        );
      }
      throw new Error("Invalid file format: Not a valid OpenXML (.xlsx) archive.");
    }

    // 2. Locate End of Central Directory (EOCD) record
    // EOCD signature is 0x06054b50 (PK\x05\x06), within the last 65557 bytes
    const tailSize = Math.min(stat.size, 65557);
    const tailBuf = Buffer.alloc(tailSize);
    await fd.read(tailBuf, 0, tailSize, stat.size - tailSize);

    let eocdOffsetInTail = -1;
    for (let i = tailSize - 22; i >= 0; i--) {
      if (tailBuf[i] === 0x50 && tailBuf[i + 1] === 0x4B && tailBuf[i + 2] === 0x05 && tailBuf[i + 3] === 0x06) {
        eocdOffsetInTail = i;
        break;
      }
    }

    if (eocdOffsetInTail === -1) {
      throw new Error("Invalid spreadsheet: Corrupted ZIP archive (missing End of Central Directory).");
    }

    const cdSize = tailBuf.readUInt32LE(eocdOffsetInTail + 12);
    const cdOffset = tailBuf.readUInt32LE(eocdOffsetInTail + 16);

    if (cdOffset + cdSize > stat.size) {
      throw new Error("Invalid spreadsheet: Corrupted ZIP archive central directory bounds.");
    }

    // Read Central Directory entries and sum uncompressed sizes
    const cdBuf = Buffer.alloc(cdSize);
    await fd.read(cdBuf, 0, cdSize, cdOffset);

    let totalUncompressedBytes = 0;
    let pos = 0;
    let entryCount = 0;

    while (pos + 46 <= cdSize) {
      if (cdBuf[pos] !== 0x50 || cdBuf[pos + 1] !== 0x4B || cdBuf[pos + 2] !== 0x01 || cdBuf[pos + 3] !== 0x02) {
        break;
      }

      entryCount++;
      if (entryCount > 10000) {
        throw new Error("ZIP Bomb protection: Archive contains excessive number of entries.");
      }

      const uncompressedSize = cdBuf.readUInt32LE(pos + 24);
      totalUncompressedBytes += uncompressedSize;

      if (totalUncompressedBytes > MAX_UNCOMPRESSED_BYTES) {
        throw new Error(`Uncompressed size (${(totalUncompressedBytes / 1024 / 1024).toFixed(1)}MB) exceeds maximum safe expansion limit of 50MB. (ZIP Bomb protection)`);
      }

      const fileNameLength = cdBuf.readUInt16LE(pos + 28);
      const extraFieldLength = cdBuf.readUInt16LE(pos + 30);
      const commentLength = cdBuf.readUInt16LE(pos + 32);

      pos += 46 + fileNameLength + extraFieldLength + commentLength;
    }

    const ratio = totalUncompressedBytes / Math.max(stat.size, 1);
    if (ratio > MAX_COMPRESSION_RATIO) {
      throw new Error(`Compression ratio (${ratio.toFixed(1)}x) exceeds safe threshold of 100x. (ZIP Bomb protection)`);
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

  // Ensure Docker sandbox runs latest host engine scripts without requiring image rebuild
  try {
    const engineRunnerSrc = path.resolve(__dirname, "engine/excel_runner.py");
    const engineRecalcSrc = path.resolve(__dirname, "engine/recalculate.py");
    if (fs.existsSync(engineRunnerSrc)) {
      await fsp.copyFile(engineRunnerSrc, path.join(tempWorkspace, "excel_runner.py"));
    }
    if (fs.existsSync(engineRecalcSrc)) {
      await fsp.copyFile(engineRecalcSrc, path.join(tempWorkspace, "recalculate.py"));
    }
  } catch (copyErr) {
    logger.warn(`Failed to copy engine scripts to temp workspace: ${copyErr.message}`);
  }

  try {
    // Phase 1: Download or copy immutable original file
    if (fileUrl) {
      let downloadUrl = String(fileUrl).trim();
      if (downloadUrl.includes("docs.google.com/spreadsheets")) {
        const sheetIdMatch = downloadUrl.match(/\/d\/([a-zA-Z0-9-_]+)/);
        if (sheetIdMatch && sheetIdMatch[1]) {
          downloadUrl = `https://docs.google.com/spreadsheets/d/${sheetIdMatch[1]}/export?format=xlsx`;
        }
      } else if (downloadUrl.includes("drive.google.com") || downloadUrl.includes("docs.google.com/file")) {
        const match = downloadUrl.match(/\/file\/d\/([a-zA-Z0-9_-]+)/) ||
                      downloadUrl.match(/[?&]id=([a-zA-Z0-9_-]+)/) ||
                      downloadUrl.match(/\/d\/([a-zA-Z0-9_-]+)/);
        if (match && match[1]) {
          downloadUrl = `https://drive.google.com/uc?export=download&id=${match[1]}&confirm=t`;
        }
      } else if (
        (downloadUrl.includes("onedrive.live.com") || downloadUrl.includes("1drv.ms")) &&
        !downloadUrl.includes("api.onedrive.com")
      ) {
        try {
          const cleanUrl = downloadUrl.replace(/[?&]download=\d+/, "");
          const b64 = Buffer.from(cleanUrl, "utf-8")
            .toString("base64")
            .replace(/=+$/, "")
            .replace(/\//g, "_")
            .replace(/\+/g, "-");
          downloadUrl = `https://api.onedrive.com/v1.0/shares/u!${b64}/root/content`;
        } catch (e) {
          logger.warn(`[ExcelEvaluator] Failed to convert OneDrive URL to Graph API: ${e.message}`);
          if (!downloadUrl.includes("download=1")) {
            const sep = downloadUrl.includes("?") ? "&" : "?";
            downloadUrl = `${downloadUrl}${sep}download=1`;
          }
        }
      } else if (downloadUrl.includes("sharepoint.com") && !downloadUrl.includes("download=1")) {
        const sep = downloadUrl.includes("?") ? "&" : "?";
        downloadUrl = `${downloadUrl}${sep}download=1`;
      }

      logger.info(`[ExcelEvaluator] Downloading submission file from URL for Job ${jobId}: ${downloadUrl}`);
      await safeDownloadSpreadsheet(downloadUrl, originalFile);

      // Verify the downloaded file is not an HTML access-denied or preview page from Google
      const sampleBuf = Buffer.alloc(512);
      const fd = await fsp.open(originalFile, "r");
      await fd.read(sampleBuf, 0, 512, 0);
      await fd.close();
      const headStr = sampleBuf.toString("utf-8");

      if (/^\s*<!DOCTYPE\s+html/i.test(headStr) || /^\s*<html/i.test(headStr)) {
        const htmlContent = await fsp.readFile(originalFile, "utf-8");
        const confirmMatch = htmlContent.match(/href="(\/uc\?export=download[^"]+confirm=[^"]+)"/i) ||
                             htmlContent.match(/href="(https:\/\/[^"]*drive\.usercontent\.google\.com\/[^"]+)"/i);
        if (confirmMatch && confirmMatch[1]) {
          let retryUrl = confirmMatch[1];
          if (retryUrl.startsWith("/")) retryUrl = `https://drive.google.com${retryUrl}`;
          logger.info(`[ExcelEvaluator] Following Google Drive interstitial confirmation link: ${retryUrl}`);
          await safeDownloadSpreadsheet(retryUrl, originalFile);
        } else {
          const isOneDrive = downloadUrl.includes("1drv.ms") || downloadUrl.includes("onedrive.live.com") || downloadUrl.includes("sharepoint.com");
          const msg = isOneDrive
            ? "The submission URL returned an HTML web page instead of an Excel file. Please ensure the OneDrive / Excel link is shared with 'Anyone with the link can view' without requiring sign-in, or submit directly as an uploaded Excel file (.xlsx)."
            : "The submission URL returned a private or restricted webpage instead of a raw Excel file. Please open the file in Google Drive/Sheets, click Share, set general access to 'Anyone with the link can view', or submit directly as an uploaded Excel file (.xlsx).";
          throw new Error(msg);
        }
      }
    } else if (providedFilePath) {
      const resolvedPath = path.resolve(providedFilePath);
      const appRoot = path.resolve(__dirname, "../../..");
      const tmpRoot = path.resolve(os.tmpdir());
      if (!resolvedPath.startsWith(appRoot) && !resolvedPath.startsWith(tmpRoot)) {
        throw new Error("Unauthorized file access: filePath must reside within temporary or application directories.");
      }
      if (!fs.existsSync(resolvedPath)) {
        throw new Error(`Provided filePath does not exist: ${providedFilePath}`);
      }
      await fsp.copyFile(resolvedPath, originalFile);
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
        logger.error(`Docker Excel execution encountered error: ${dockerErr.message}. Falling back to local engine.`);
        try {
          report = await runLocalExcelEngine(originalFile, recalculatedFile, specFile, profileDir);
        } catch (localErr) {
          throw new Error(`Docker Excel execution failed (${dockerErr.message}) and local fallback failed (${localErr.message}). Build the image with 'docker build -t eval-excel-sandbox -f docker/Dockerfile.excel .'`);
        }
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
