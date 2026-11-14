import fs from 'fs';
import fsp from 'fs/promises';
import path from 'path';
import { fileURLToPath } from 'url';
import { exec } from 'child_process';
import util from 'util';
import { Readable } from 'stream';
import logger from '../../config/logger.js';
import { assertSafeUrl } from '../visual/utils/urlGuard.js';

const execPromise = util.promisify(exec);
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const EXTRACT_SQL_ZIP_SCRIPT = path.resolve(__dirname, 'engine/extract_sql_zip.py');

const MAX_REDIRECT_HOPS = 3;

/**
 * Safely downloads a remote .sql file with:
 * - Manual redirect following with SSRF re-validation at each hop
 * - Streaming response to disk to prevent unbounded heap memory buffering
 * - Hard byte limit enforcement
 *
 * @param {string} targetUrl  - The URL to download from
 * @param {string} destPath   - Absolute path to save the downloaded file
 * @param {number} maxBytes   - Maximum allowed file size in bytes
 */
async function safeDownloadSql(targetUrl, destPath, maxBytes) {
  let currentUrl = targetUrl;
  let res = null;

  for (let hop = 0; hop <= MAX_REDIRECT_HOPS; hop++) {
    await assertSafeUrl(currentUrl);

    res = await fetch(currentUrl, {
      redirect: 'manual',
      headers: {
        'User-Agent': 'CodeGuru-Evaluator/1.0'
      }
    });

    if ([301, 302, 303, 307, 308].includes(res.status)) {
      const location = res.headers.get('location');
      if (!location) {
        throw new Error(`Received HTTP ${res.status} redirect without Location header.`);
      }
      currentUrl = new URL(location, currentUrl).href;
      continue;
    }

    if ([503, 429].includes(res.status)) {
      // Retry once after delay for transient rate limits or 503
      await new Promise(r => setTimeout(r, 1500));
      res = await fetch(currentUrl, {
        redirect: 'manual',
        headers: { 'User-Agent': 'CodeGuru-Evaluator/1.0' }
      });
    }

    break;
  }

  if (!res || !res.ok) {
    const status = res ? res.status : 'Unknown';
    throw new Error(`Failed to download SQL file (HTTP ${status}).`);
  }

  // Check Content-Length if provided
  const contentLength = parseInt(res.headers.get('content-length') || '0', 10);
  if (contentLength > maxBytes) {
    throw new Error(`SQL file too large: ${contentLength} bytes exceeds ${maxBytes} byte limit.`);
  }

  // Stream to disk with size enforcement
  const body = res.body;
  if (!body) {
    throw new Error('Response body is empty.');
  }

  const writeStream = fs.createWriteStream(destPath);
  let bytesWritten = 0;

  const nodeStream = Readable.fromWeb(body);
  for await (const chunk of nodeStream) {
    bytesWritten += chunk.length;
    if (bytesWritten > maxBytes) {
      writeStream.destroy();
      await fsp.unlink(destPath).catch(() => {});
      throw new Error(`SQL file exceeds ${maxBytes} byte limit during download.`);
    }
    writeStream.write(chunk);
  }

  writeStream.end();
  await new Promise((resolve, reject) => {
    writeStream.on('finish', resolve);
    writeStream.on('error', reject);
  });
}

/**
 * Cleans SQL submissions that students copy-pasted directly from interactive
 * psql terminal/command prompt sessions (e.g. lines with "postgres=# SELECT ...",
 * "Server [localhost]:", "\l", "\c", "\q", output tables, etc.).
 *
 * @param {string} rawSql
 * @returns {string}
 */
export function cleanSqlSubmission(rawSql) {
  if (!rawSql || typeof rawSql !== 'string') return '';
  
  // 1. Strip UTF-8 BOM (\uFEFF) if present
  let text = rawSql.replace(/^\uFEFF/, '').trim();

  // 2. Detect pgAdmin ERD diagram files (.pgerd JSON) or raw JSON uploads
  if (text.startsWith('{')) {
    let isJson = false;
    try {
      const parsed = JSON.parse(text);
      if (parsed && typeof parsed === 'object') isJson = true;
    } catch {
      if (text.includes('"tables"') || text.includes('"columns"') || text.includes('"diagram"') || text.includes('defaultstorage') || text.includes('pg_catalog')) {
        isJson = true;
      }
    }
    if (isJson) {
      throw new Error(
        "You submitted a pgAdmin ERD Diagram file (.pgerd) or JSON file instead of a SQL script. To submit your assignment, open pgAdmin, open your ERD or Query Tool, click 'Generate SQL' (or write your CREATE TABLE script), and save/upload your work as a .sql script file."
      );
    }
  }

  // 3. Detect binary PostgreSQL backup files (PGDMP)
  if (text.startsWith('PGDMP') || text.charCodeAt(0) === 0) {
    throw new Error(
      "You submitted a binary PostgreSQL database dump file. Please export and submit plain-text SQL scripts (.sql)."
    );
  }

  // 4. Strip outer markdown code blocks (e.g. ```sql ... ```)
  const codeBlockMatch = text.match(/^```(?:sql)?\s*([\s\S]*?)\s*```$/i);
  if (codeBlockMatch && codeBlockMatch[1]) {
    text = codeBlockMatch[1].trim();
  }

  // 5. Detect terminal transcript artifacts (e.g. psql / MySQL / SQLite CLI shell copy-paste)
  const isTerminalTranscript = /(?:Server\s*\[|Database\s*\[|Port\s*\[|Username\s*\[|Password for user|psql\s*\(|postgres[#=>]|mysql>|sqlite>|SQL>|\b\\[a-z]+\b|\(\d+\s+rows?\))/i.test(text);
  if (!isTerminalTranscript) {
    return text;
  }

  logger.info('[SqlLoader] Detected interactive CLI / terminal transcript in submission. Cleaning and isolating SQL...');

  const lines = text.split(/\r?\n/);
  const cleanedLines = [];
  let inTerminalOutput = false;

  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed) {
      cleanedLines.push('');
      continue;
    }

    // A. Check if line starts with a CLI prompt (psql, MySQL, SQLite, PowerShell, cmd, bash)
    // Note: Avoid matching SQL '--' comments
    const PROMPT_REGEX = /^(?:PS\s+[A-Z]:\\[^>]*>|[A-Z]:\\[^>]*>|[\w.-]+@[\w.-]+:[^$#]*[$#]|[a-zA-Z0-9_]+(?:\(#|=[#>]|-[#>]|[#=>])|mysql>|sqlite>|SQL>|\d+>|\$)\s*/i;
    const hasPrompt = PROMPT_REGEX.test(trimmed);
    let stripped = trimmed.replace(PROMPT_REGEX, '');

    if (hasPrompt) {
      inTerminalOutput = false;
    }

    // B. Real SQL comments: preserve completely intact (exclude ASCII table dividers like '----')
    const isAsciiTableBorder = /^[\+\-|:=_\s]{3,}$/.test(stripped) && !stripped.includes('*');
    if (!isAsciiTableBorder && (stripped.startsWith('--') || stripped.startsWith('/*'))) {
      inTerminalOutput = false;
      cleanedLines.push(stripped);
      continue;
    }

    // C. Detect psql / database slash meta-commands (\l, \c, \q, \dt, \d+, \x, etc.)
    if (/^\\(?:l|c|q|dt|d|dn|df|du|x|timing|connect|copy|echo)\b/i.test(stripped)) {
      inTerminalOutput = /^\\(?:l|dt|d|dn|df|du)\b/i.test(stripped);
      cleanedLines.push(`-- [Terminal Command] ${stripped}`);
      continue;
    }

    // D. Detect terminal setup prompts, connection banners, shell commands & PG status/warning lines
    if (/^(?:Server\s*\[|Database\s*\[|Port\s*\[|Username\s*\[|Password(?:\s+for\s+user(?:\s+[^:]+)?)?\s*:|psql\s*\(|psql\s+-|Type\s+"help"|SSL\s+connection|You\s+are\s+now\s+connected|8-bit\s+characters\s+might\s+not\s+work|See\s+psql\s+reference\s+page|page\s+"Notes\s+for\s+Windows\s+users"|Press\s+any\s+key\s+to\s+continue|List\s+of\s+(?:databases|relations|schemas|roles)|(?:NOTICE|WARNING|DETAIL|HINT|CONTEXT|INFO)\s*:|(?:sudo|apt(?:-get)?|brew|pg_ctl|systemctl|service)\s+|(?:INSERT\s+0\s+\d+|CREATE\s+TABLE|ALTER\s+TABLE|DROP\s+TABLE)\s*$)/i.test(stripped)) {
      inTerminalOutput = /^List\s+of\s+/i.test(stripped);
      cleanedLines.push(`-- [Terminal Prompt] ${stripped}`);
      continue;
    }

    // E. Known SQL statement start keywords
    if (/^(?:SELECT|WITH|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|SHOW|EXPLAIN|CALL|EXEC|SET)\b/i.test(stripped)) {
      inTerminalOutput = false;
      cleanedLines.push(stripped);
      if (stripped.includes(';')) {
        inTerminalOutput = true; // Table output commonly follows a terminated SELECT query in psql
      }
      continue;
    }

    // F. Detect ASCII table borders & row counts
    const isAsciiRowCount = /^\(\d+\s+rows?\)$/i.test(stripped) || /^Query\s+OK,?\s+\d+/i.test(stripped);

    if (inTerminalOutput || isAsciiTableBorder || isAsciiRowCount) {
      cleanedLines.push(`-- [Terminal Output] ${trimmed}`);
      continue;
    }

    // G. Normal SQL continuation line (e.g. within a multi-line query)
    cleanedLines.push(stripped);
    if (stripped.includes(';')) {
      inTerminalOutput = true;
    }
  }

  const cleanedResult = cleanedLines.join('\n').trim();
  logger.info('[SqlLoader] Terminal transcript successfully sanitized into executable SQL.');
  return cleanedResult;
}

/**
 * Loads the student's SQL submission from one of these sources (checked in order):
 * 1. Raw SQL text (sqlText) — from in-browser editor submissions
 * 2. IDE files (ideFiles) — array of { name, content } from web IDE
 * 3. Remote URL (fileUrl) — S3 presigned URL, GitHub raw URL, etc.
 *
 * @param {object} options
 * @param {string}   [options.sqlText]       - Raw SQL string from web editor
 * @param {Array}    [options.ideFiles]      - Array of { name, content } from IDE
 * @param {string}   [options.fileUrl]       - Remote URL to download .sql file from
 * @param {string}   options.tempWorkspace   - Temporary directory for file downloads
 * @param {number}   options.maxSizeBytes    - Maximum allowed SQL file size
 * @returns {Promise<string>}               - The student's SQL content as a string
 */
export async function loadSqlSubmission({ sqlText, ideFiles, fileUrl, tempWorkspace, maxSizeBytes, assignmentTitle = '', manifest = null }) {
  // Priority 1: Inline SQL text from in-browser editor
  if (typeof sqlText === 'string' && sqlText.trim().length > 0) {
    logger.info('[SqlLoader] Using inline sqlText from submission payload');

    if (Buffer.byteLength(sqlText, 'utf-8') > maxSizeBytes) {
      throw new Error(`Inline SQL text exceeds ${maxSizeBytes} byte limit.`);
    }

    return cleanSqlSubmission(sqlText);
  }

  // Priority 2: IDE files (array of { name, content })
  if (Array.isArray(ideFiles) && ideFiles.length > 0) {
    logger.info(`[SqlLoader] Using ideFiles (${ideFiles.length} file(s)) from submission payload`);

    // Find the .sql file, or use the first file
    const sqlFile = ideFiles.find(f => f.name && f.name.endsWith('.sql')) || ideFiles[0];

    if (!sqlFile || typeof sqlFile.content !== 'string') {
      throw new Error('ideFiles entry is missing content.');
    }

    if (Buffer.byteLength(sqlFile.content, 'utf-8') > maxSizeBytes) {
      throw new Error(`IDE file ${sqlFile.name || 'unknown'} exceeds ${maxSizeBytes} byte limit.`);
    }

    return cleanSqlSubmission(sqlFile.content);
  }

  // Priority 3: Remote URL download
  if (typeof fileUrl === 'string' && fileUrl.trim().length > 0) {
    const cleanUrl = fileUrl.trim();
    logger.info(`[SqlLoader] Downloading SQL file from URL: ${cleanUrl}`);

    const destPath = path.join(tempWorkspace, 'student_submission.sql');

    let downloadUrl = cleanUrl;

    // Convert Google Drive view/share URLs and Google Docs to direct download
    if (downloadUrl.includes('docs.google.com/document/')) {
      const docMatch = downloadUrl.match(/\/document\/d\/([a-zA-Z0-9_-]+)/);
      if (docMatch && docMatch[1]) {
        downloadUrl = `https://docs.google.com/document/d/${docMatch[1]}/export?format=txt`;
        logger.info(`[SqlLoader] Converted Google Docs URL to raw text export: ${downloadUrl}`);
      }
    } else if (downloadUrl.includes('drive.google.com') || downloadUrl.includes('docs.google.com')) {
      const match = downloadUrl.match(/\/file\/d\/([a-zA-Z0-9_-]+)/) ||
                    downloadUrl.match(/[?&]id=([a-zA-Z0-9_-]+)/) ||
                    downloadUrl.match(/\/d\/([a-zA-Z0-9_-]+)/);
      if (match && match[1]) {
        downloadUrl = `https://drive.google.com/uc?export=download&id=${match[1]}&confirm=t`;
        logger.info(`[SqlLoader] Converted Google Drive view URL to direct download: ${downloadUrl}`);
      }
    }

/**
 * Resolves SQL content from a GitHub repository, tree, or file URL.
 * Automatically discovers all .sql files in the repository and concatenates them
 * in logical order (DDL -> DML -> Queries).
 *
 * @param {string} gitHubUrl
 * @param {number} maxBytes
 * @returns {Promise<{ combinedSql?: string, directRawUrl?: string } | null>}
 */
async function resolveGitHubSqlSubmission(gitHubUrl, maxBytes, matchContext = {}) {
  const { assignmentTitle = '', manifest = null } = matchContext;
  // 1. Direct raw file link — let standard downloader fetch it directly
  if (gitHubUrl.includes('raw.githubusercontent.com')) {
    return null;
  }

  // 2. Direct blob link: github.com/owner/repo/blob/branch/path/to/file.sql
  const blobMatch = gitHubUrl.match(/github\.com\/([^/]+)\/([^/]+)\/blob\/([^/]+)\/(.+\.sql)$/i);
  if (blobMatch) {
    const [, owner, repo, branch, filePath] = blobMatch;
    const rawUrl = `https://raw.githubusercontent.com/${owner}/${repo}/${branch}/${filePath}`;
    logger.info(`[SqlLoader] Converted GitHub blob URL to raw URL: ${rawUrl}`);
    return { directRawUrl: rawUrl };
  }

  // 3. Repository root or tree: github.com/owner/repo or github.com/owner/repo/tree/branch
  const repoMatch = gitHubUrl.match(/github\.com\/([^/]+)\/([^/]+)(?:\/tree\/([^/]+))?/i);
  if (!repoMatch) return null;

  const [, owner, rawRepo, explicitBranch] = repoMatch;
  const repo = rawRepo.replace(/\.git$/, '');

  logger.info(`[SqlLoader] Scanning GitHub repository ${owner}/${repo} for SQL files...`);

  // Try branches: explicit branch, main, master, dev
  const branchesToTry = explicitBranch ? [explicitBranch] : ['main', 'master', 'dev'];

  for (const branch of branchesToTry) {
    try {
      const treeApiUrl = `https://api.github.com/repos/${owner}/${repo}/git/trees/${branch}?recursive=1`;
      const res = await fetch(treeApiUrl, {
        headers: {
          'User-Agent': 'CodeGuru-Evaluator/1.0',
          'Accept': 'application/vnd.github.v3+json'
        }
      });

      if (res.status === 404) {
        continue; // Try next branch
      }

      if (res.status === 403 || res.status === 429) {
        logger.warn(`[SqlLoader] GitHub API rate limit/forbidden (status ${res.status}). Probing common SQL filenames.`);
        break;
      }

      if (!res.ok) continue;

      const data = await res.json();
      if (!Array.isArray(data.tree)) continue;

      // Filter for .sql files, excluding node_modules, .git, etc.
      const sqlFiles = data.tree.filter(
        item => item.type === 'blob' &&
                item.path.toLowerCase().endsWith('.sql') &&
                !item.path.includes('node_modules/') &&
                !item.path.startsWith('.')
      );

      if (sqlFiles.length === 0) {
        throw new Error(
          `No .sql files were found in GitHub repository https://github.com/${owner}/${repo} on branch '${branch}'. Please commit and push your .sql solution files to the repository.`
        );
      }

      logger.info(`[SqlLoader] Found ${sqlFiles.length} SQL file(s) in repository: ${sqlFiles.map(f => f.path).join(', ')}`);

      // Sort files: DDL (schema/create/table) -> DML (insert/data/seed) -> DQL (query/select/solution)
      sqlFiles.sort((a, b) => {
        const pA = a.path.toLowerCase();
        const pB = b.path.toLowerCase();
        const score = (p) => {
          if (p.includes('schema') || p.includes('create') || p.includes('table') || p.includes('ddl')) return 1;
          if (p.includes('seed') || p.includes('data') || p.includes('insert') || p.includes('dml')) return 2;
          return 3;
        };
        return score(pA) - score(pB);
      });

      // Download each file's content
      const contents = [];
      let totalBytes = 0;

      for (const file of sqlFiles) {
        const rawFileUrl = `https://raw.githubusercontent.com/${owner}/${repo}/${branch}/${file.path}`;
        const fileRes = await fetch(rawFileUrl, {
          headers: { 'User-Agent': 'CodeGuru-Evaluator/1.0' }
        });

        if (!fileRes.ok) continue;
        const text = await fileRes.text();
        const trimmed = text.trim();

        // Skip pgAdmin ERD diagram JSON files masquerading as .sql
        if (trimmed.startsWith('{') && (trimmed.includes('"version"') || trimmed.includes('"layers"') || trimmed.includes('"data"')) && trimmed.endsWith('}')) {
          logger.info(`[SqlLoader] Skipping '${file.path}': detected pgAdmin ERD diagram JSON content disguised with .sql extension.`);
          continue;
        }

        totalBytes += Buffer.byteLength(text, 'utf-8');

        if (totalBytes > maxBytes) {
          throw new Error(`Total SQL content in repository exceeds ${maxBytes} byte limit.`);
        }

        contents.push({ path: file.path, text, formatted: `-- File: ${file.path}\n${text}` });
      }

      if (contents.length > 0) {
        if (contents.length > 1 && (assignmentTitle || manifest)) {
          const stop = new Set(['practice', 'steps', 'assignment', 'module', 'challenge', 'environment', 'using', 'local', 'your', 'with', 'from', 'into', 'table', 'select', 'create']);
          const metaText = `${assignmentTitle || ''} ${manifest?.schema || ''} ${manifest?.referenceQuery || ''} ${manifest?.referenceScript || ''}`.toLowerCase();
          const keywords = [...new Set((metaText.match(/[a-z_]{4,}/g) || []).filter((w) => !stop.has(w)))];
          if (keywords.length > 0) {
            const scored = contents.map((c) => {
              const pLow = c.path.toLowerCase();
              const tLow = c.text.toLowerCase();
              let s = 0;
              for (const kw of keywords) {
                if (pLow.includes(kw)) s += 3;
                if (tLow.includes(kw)) s += 1;
              }
              return { ...c, s };
            });
            const maxS = Math.max(...scored.map((x) => x.s));
            if (maxS > 0) {
              const filtered = scored.filter((x) => x.s >= Math.ceil(maxS * 0.6));
              if (filtered.length > 0 && filtered.length < contents.length) {
                logger.info(`[SqlLoader] Smart-selected ${filtered.length}/${contents.length} matching GitHub SQL file(s): ${filtered.map((f) => f.path).join(', ')}`);
                return { combinedSql: filtered.map((f) => f.formatted).join('\n\n') };
              }
            }
          }
        }
        return { combinedSql: contents.map((c) => c.formatted).join('\n\n') };
      }
    } catch (err) {
      if (err.message.includes('No .sql files') || err.message.includes('exceeds')) {
        throw err;
      }
      logger.warn(`[SqlLoader] Error querying branch '${branch}' for ${owner}/${repo}: ${err.message}`);
    }
  }

  // Fallback: probe common raw SQL filenames directly
  const commonNames = ['solution.sql', 'queries.sql', 'schema.sql', 'submission.sql', 'main.sql', 'assignment.sql', 'script.sql'];
  for (const branch of ['main', 'master']) {
    for (const name of commonNames) {
      try {
        const rawUrl = `https://raw.githubusercontent.com/${owner}/${repo}/${branch}/${name}`;
        const probe = await fetch(rawUrl, {
          headers: { 'User-Agent': 'CodeGuru-Evaluator/1.0' }
        });
        if (probe.ok) {
          const text = await probe.text();
          if (text && text.trim().length > 0) {
            logger.info(`[SqlLoader] Successfully fetched fallback SQL file ${name} from ${branch}`);
            return { combinedSql: text };
          }
        }
      } catch {}
    }
  }

  throw new Error(
    `Unable to retrieve SQL files from GitHub repository https://github.com/${owner}/${repo}. Please verify that the repository is PUBLIC and contains your .sql files.`
  );
}

    // Handle GitHub repository URLs — automatically discover & extract SQL files
    if (downloadUrl.includes('github.com')) {
      const gitHubResult = await resolveGitHubSqlSubmission(downloadUrl, maxSizeBytes, { assignmentTitle, manifest });
      if (gitHubResult?.combinedSql) {
        logger.info(`[SqlLoader] Successfully extracted SQL from GitHub repository: ${downloadUrl}`);
        return cleanSqlSubmission(gitHubResult.combinedSql);
      }
      if (gitHubResult?.directRawUrl) {
        downloadUrl = gitHubResult.directRawUrl;
      }
    }

    await safeDownloadSql(downloadUrl, destPath, maxSizeBytes);

    // Check if the downloaded file is a binary image or document (e.g. PNG, JPG, PDF) uploaded instead of SQL
    const fileHeader = Buffer.alloc(8);
    const fd = await fsp.open(destPath, 'r');
    await fd.read(fileHeader, 0, 8, 0);
    await fd.close();

    // PNG: 89 50 4E 47 | JPEG: FF D8 FF | PDF: 25 50 44 46 (%PDF)
    const isPng = fileHeader[0] === 0x89 && fileHeader[1] === 0x50 && fileHeader[2] === 0x4E && fileHeader[3] === 0x47;
    const isJpeg = fileHeader[0] === 0xFF && fileHeader[1] === 0xD8 && fileHeader[2] === 0xFF;
    const isPdf = fileHeader[0] === 0x25 && fileHeader[1] === 0x50 && fileHeader[2] === 0x44 && fileHeader[3] === 0x46;
    if (isPng || isJpeg || isPdf) {
      const typeName = isPng ? 'PNG image' : (isJpeg ? 'JPEG image' : 'PDF document');
      throw new Error(
        `You submitted a ${typeName} screenshot instead of a SQL script file. Automated grading requires an executable .sql or .txt script file. Please copy your SQL code into a text file, save it as your_work.sql, and re-submit.`
      );
    }

    // Check if the downloaded file is a ZIP archive (starts with PK\x03\x04 or URL contains .zip)
    if (fileHeader[0] === 0x50 && fileHeader[1] === 0x4B) {
      logger.info(`[SqlLoader] Detected ZIP archive submission. Extracting SQL files from ${destPath}...`);
      const pythonBin = process.platform === 'win32' ? 'python' : 'python3';
      const { stdout } = await execPromise(`"${pythonBin}" "${EXTRACT_SQL_ZIP_SCRIPT}" "${destPath}"`, { timeout: 30000 });
      return cleanSqlSubmission(stdout);
    }

    let content = await fsp.readFile(destPath, 'utf-8');

    // If Google Drive returned an interstitial HTML page (e.g. large file virus check confirmation),
    // extract the actual download link and follow it
    if (/^\s*<!DOCTYPE\s+html/i.test(content) || /^\s*<html/i.test(content)) {
      const confirmMatch = content.match(/href="(\/uc\?export=download[^"]+confirm=[^"]+)"/i) ||
                           content.match(/href="(https:\/\/[^"]*drive\.usercontent\.google\.com\/[^"]+)"/i);
      if (confirmMatch && confirmMatch[1]) {
        let retryUrl = confirmMatch[1];
        if (retryUrl.startsWith('/')) {
          retryUrl = `https://drive.google.com${retryUrl}`;
        }
        logger.info(`[SqlLoader] Following Google Drive interstitial confirmation URL: ${retryUrl}`);
        try {
          await safeDownloadSql(retryUrl, destPath, maxSizeBytes);
          content = await fsp.readFile(destPath, 'utf-8');
        } catch (retryErr) {
          logger.warn(`[SqlLoader] Retry via confirmation link failed: ${retryErr.message}`);
        }
      }
    }

    // Safety guard: if Google Drive, GitHub, or remote server still returned an HTML error/preview page instead of SQL
    if (/^\s*<!DOCTYPE\s+html/i.test(content) || /^\s*<html/i.test(content)) {
      if (cleanUrl.includes('github.com')) {
        throw new Error(
          'The GitHub submission URL returned a web page instead of raw SQL code. Please make sure your repository is Public and contains valid .sql files, or submit a direct link to your SQL file.'
        );
      }
      throw new Error(
        'The submission URL returned a private or restricted Google Drive webpage instead of a raw SQL file. Please open the file in Google Drive, click Share, change "General access" to "Anyone with the link can view", and re-submit.'
      );
    }

    return cleanSqlSubmission(content);
  }

  // No SQL content found
  logger.warn('[SqlLoader] No SQL content found in submission payload');
  return '';
}

