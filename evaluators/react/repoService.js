import path from 'path';
import fs from 'fs';
import os from 'os';
import { fileURLToPath } from 'url';
import { exec } from 'child_process';
import util from 'util';
import { Readable } from 'stream';
import { simpleGit } from 'simple-git';
import { assertSafeUrl, getAllowedGitHosts } from '../visual/utils/urlGuard.js';
import { parseGithubTreeUrl } from '../backend/extractService.js';
import logger from '../../config/logger.js';

const execPromise = util.promisify(exec);
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const SAFE_UNZIP_SCRIPT = path.join(__dirname, 'utils', 'safe_unzip.py');
const TEMP_DIR = path.join(process.cwd(), 'temp');
const MAX_ARCHIVE_BYTES = 50 * 1024 * 1024; // 50MB

function isArchiveTarget(target) {
  if (!target || typeof target !== 'string') return false;
  const trimmed = target.trim();
  if (trimmed.endsWith('.zip') || trimmed.endsWith('.tar.gz') || trimmed.endsWith('.tgz')) {
    return true;
  }
  try {
    const parsed = new URL(trimmed);
    const host = parsed.hostname.toLowerCase();
    if (host.includes('drive.google.com') || host.includes('docs.google.com')) {
      return true;
    }
    const pathLower = parsed.pathname.toLowerCase();
    return (
      pathLower.endsWith('.zip') ||
      pathLower.endsWith('.tar.gz') ||
      pathLower.endsWith('.tgz') ||
      parsed.search.toLowerCase().includes('.zip')
    );
  } catch {
    return false;
  }
}

async function extractZipToFolder(archivePath, destFolder) {
  const pythonBin = process.platform === 'win32' ? 'python' : 'python3';
  await execPromise(`"${pythonBin}" "${SAFE_UNZIP_SCRIPT}" "${archivePath}" "${destFolder}"`, {
    timeout: 45000,
  });
}

async function downloadAndExtractArchive(archiveUrl, destFolder) {
  let downloadUrl = archiveUrl;
  if (downloadUrl.includes('drive.google.com') || downloadUrl.includes('docs.google.com')) {
    const match = downloadUrl.match(/\/file\/d\/([a-zA-Z0-9_-]+)/) ||
                  downloadUrl.match(/[?&]id=([a-zA-Z0-9_-]+)/) ||
                  downloadUrl.match(/\/d\/([a-zA-Z0-9_-]+)/);
    if (match && match[1]) {
      downloadUrl = `https://drive.google.com/uc?export=download&id=${match[1]}&confirm=t`;
    }
  }

  await assertSafeUrl(downloadUrl);
  if (!fs.existsSync(TEMP_DIR)) {
    fs.mkdirSync(TEMP_DIR, { recursive: true });
  }

  const { randomUUID } = await import('crypto');
  const tempZipPath = path.join(os.tmpdir(), `archive_${Date.now()}_${randomUUID()}.tmp`);

  logger.info(`[repoService] Downloading project archive/file from remote: ${downloadUrl}`);
  let res = await fetch(downloadUrl, {
    redirect: 'follow',
    headers: {
      'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
    }
  });

  if (!res.ok) {
    throw new Error(`Failed to download project archive (HTTP ${res.status}): ${res.statusText}`);
  }

  // Handle Google Drive interstitial confirmation page if encountered
  const contentType = res.headers.get('content-type') || '';
  if (contentType.includes('text/html') && downloadUrl.includes('drive.google.com')) {
    const htmlText = await res.text();
    const confirmMatch = htmlText.match(/href="(\/uc\?export=download[^"]+)"/i) ||
                         htmlText.match(/href="(https:\/\/[^"]*drive\.usercontent\.google\.com\/[^"]+)"/i);
    if (confirmMatch && confirmMatch[1]) {
      let retryUrl = confirmMatch[1].replace(/&amp;/g, '&');
      if (retryUrl.startsWith('/')) retryUrl = `https://drive.google.com${retryUrl}`;
      logger.info(`[repoService] Following Google Drive interstitial confirmation URL: ${retryUrl}`);
      res = await fetch(retryUrl, {
        headers: { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36' }
      });
    } else {
      // Re-create stream from htmlText if not an interstitial
      const writeStream = fs.createWriteStream(tempZipPath);
      writeStream.write(htmlText);
      writeStream.end();
      await new Promise((r) => writeStream.once('finish', r));
    }
  }

  if (!fs.existsSync(tempZipPath)) {
    const writeStream = fs.createWriteStream(tempZipPath);
    let bytesDownloaded = 0;
    try {
      const readable = Readable.fromWeb(res.body);
      for await (const chunk of readable) {
        bytesDownloaded += chunk.length;
        if (bytesDownloaded > MAX_ARCHIVE_BYTES) {
          throw new Error(`Project archive size exceeds maximum limit of 50MB.`);
        }
        if (!writeStream.write(chunk)) {
          await new Promise((r) => writeStream.once('drain', r));
        }
      }
    } finally {
      writeStream.end();
      await new Promise((r) => writeStream.once('finish', r));
    }
  }

  try {
    if (!fs.existsSync(destFolder)) {
      fs.mkdirSync(destFolder, { recursive: true });
    }

    // Inspect file header for ZIP magic bytes (PK\x03\x04)
    const header = Buffer.alloc(4);
    const fd = fs.openSync(tempZipPath, 'r');
    fs.readSync(fd, header, 0, 4, 0);
    fs.closeSync(fd);

    const isZip = header[0] === 0x50 && header[1] === 0x4B && (header[2] === 0x03 || header[2] === 0x05);

    if (isZip) {
      logger.info(`[repoService] Extracting ZIP archive to ${destFolder}`);
      await extractZipToFolder(tempZipPath, destFolder);
    } else {
      // Standalone text/code file (e.g. .py or .html or .js)
      const sample = fs.readFileSync(tempZipPath, 'utf8').slice(0, 500).toLowerCase();
      let targetName = 'solution.py';
      if (sample.includes('<!doctype html') || sample.includes('<html')) {
        targetName = 'index.html';
      } else if (sample.includes('def ') || sample.includes('import ') || sample.includes('print(')) {
        targetName = 'main.py';
      } else if (sample.includes('function') || sample.includes('const ') || sample.includes('let ')) {
        targetName = 'index.js';
      }
      logger.info(`[repoService] Standalone code file detected; copying to ${targetName}`);
      fs.copyFileSync(tempZipPath, path.join(destFolder, targetName));
    }
  } finally {
    try { fs.unlinkSync(tempZipPath); } catch {}
  }
}

function isRemoteFile(target) {
  if (!target || typeof target !== 'string') return false;
  const trimmed = target.trim();
  try {
    const parsed = new URL(trimmed);
    const host = parsed.hostname.toLowerCase();
    const pathLower = parsed.pathname.toLowerCase();
    const isS3 = host.includes('amazonaws.com') || host.includes('s3');
    const isCodeExt = /\.(js|ts|py|html|css|json|txt|jsx|tsx|sh|sql)$/i.test(pathLower);
    return isS3 || isCodeExt;
  } catch {
    return false;
  }
}

async function downloadSingleRemoteFile(fileUrl, destFolder) {
  await assertSafeUrl(fileUrl);
  if (!fs.existsSync(destFolder)) {
    fs.mkdirSync(destFolder, { recursive: true });
  }

  let fileName = 'solution.js';
  try {
    const parsed = new URL(fileUrl);
    const base = path.basename(parsed.pathname);
    if (base && base.includes('.')) {
      fileName = base.replace(/^[0-9]+-/, '');
    }
  } catch {}

  const destFile = path.join(destFolder, fileName);
  logger.info(`[repoService] Downloading standalone file from S3/remote to ${destFile}: ${fileUrl}`);

  const res = await fetch(fileUrl, {
    headers: {
      'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
    }
  });

  if (!res.ok) {
    throw new Error(`Failed to download submitted file (HTTP ${res.status}): ${res.statusText}`);
  }

  const writeStream = fs.createWriteStream(destFile);
  let bytesDownloaded = 0;
  try {
    const readable = Readable.fromWeb(res.body);
    for await (const chunk of readable) {
      bytesDownloaded += chunk.length;
      if (bytesDownloaded > MAX_ARCHIVE_BYTES) {
        throw new Error(`File size exceeds maximum limit of 50MB.`);
      }
      if (!writeStream.write(chunk)) {
        await new Promise((r) => writeStream.once('drain', r));
      }
    }
  } finally {
    writeStream.end();
    await new Promise((r) => writeStream.once('finish', r));
  }
}

/**
 * Clones a repository or prepares a local project directory.
 * Supports:
 * 1. Standard Git repository URLs
 * 2. GitHub tree URLs with subfolders (e.g. https://github.com/user/repo/tree/main/client)
 * 3. Already-materialized local directories (e.g. in-browser IDE submissions)
 * 4. Direct ZIP archive uploads from S3 or local disk
 * 5. Standalone code files from S3 or local disk
 * 6. Automatic monorepo subfolder detection if package.json is in a subfolder
 *
 * @param {string} repoUrl - Remote URL or local directory path
 * @returns {Promise<string>} - Absolute path to the project root
 */
export async function cloneRepo(repoUrl, options = {}) {
  if (!repoUrl) {
    throw new Error('repoUrl or local project path is required');
  }

  // 1. If repoUrl is already an existing local directory (e.g. IDE submission)
  if (fs.existsSync(repoUrl) && fs.statSync(repoUrl).isDirectory()) {
    logger.info(`Using pre-existing local directory for evaluation: ${repoUrl}`);
    return repoUrl;
  }

  if (!fs.existsSync(TEMP_DIR)) {
    fs.mkdirSync(TEMP_DIR, { recursive: true });
  }

  const { randomUUID } = await import('crypto');
  const repoName = `repo_${Date.now()}_${randomUUID()}`;
  const repoPath = path.join(TEMP_DIR, repoName);

  // 2. Direct Archive Upload (.zip / S3 Presigned URL) or Standalone file
  if (isArchiveTarget(repoUrl)) {
    if (fs.existsSync(repoUrl) && !fs.statSync(repoUrl).isDirectory()) {
      logger.info(`Extracting local archive file: ${repoUrl}`);
      await extractZipToFolder(path.resolve(repoUrl), repoPath);
    } else {
      await downloadAndExtractArchive(repoUrl, repoPath);
    }
  } else if (isRemoteFile(repoUrl)) {
    if (fs.existsSync(repoUrl) && !fs.statSync(repoUrl).isDirectory()) {
      fs.mkdirSync(repoPath, { recursive: true });
      fs.copyFileSync(repoUrl, path.join(repoPath, path.basename(repoUrl)));
    } else {
      await downloadSingleRemoteFile(repoUrl, repoPath);
    }
  } else {
    // 3. Parse GitHub tree / subfolder URLs
    const parsedGithub = parseGithubTreeUrl(repoUrl);
    const actualCloneUrl = parsedGithub ? parsedGithub.cloneUrl : repoUrl;

    await assertSafeUrl(actualCloneUrl, { allowedHosts: getAllowedGitHosts() });

    const cloneArgs = ['--depth', '1'];
    if (parsedGithub && parsedGithub.branch) {
      cloneArgs.push('--branch', parsedGithub.branch);
    }
    cloneArgs.push('--');

    await simpleGit({ env: { ...process.env, GIT_TERMINAL_PROMPT: '0' } }).clone(actualCloneUrl, repoPath, cloneArgs);

    // 4. Resolve explicit subpath if parsed from a GitHub tree URL
    if (parsedGithub && parsedGithub.subPath) {
      const explicitSubPath = path.resolve(repoPath, parsedGithub.subPath);
      if (explicitSubPath.startsWith(repoPath) && fs.existsSync(explicitSubPath)) {
        logger.info(`Resolved explicit subfolder from GitHub URL: ${parsedGithub.subPath}`);
        return explicitSubPath;
      }
    }
  }

  // 4. Auto-detect subfolder if root has no package.json/src, but a subfolder does (Monorepo detection)
  // CRITICAL: If options.preserveRoot is true, OR if there are multiple subprojects (e.g. backend AND frontend),
  // NEVER narrow down to a single subfolder, because fullstack evaluators require the whole repo.
  const hasRootPackage = fs.existsSync(path.join(repoPath, 'package.json'));
  const hasRootSrc = fs.existsSync(path.join(repoPath, 'src'));

  if (!hasRootPackage && !hasRootSrc && !options.preserveRoot) {
    const entries = fs.readdirSync(repoPath, { withFileTypes: true }).filter((e) => e.isDirectory() && !e.name.startsWith('.'));
    const subprojects = entries.filter((entry) => {
      const subCandidate = path.join(repoPath, entry.name);
      return (
        fs.existsSync(path.join(subCandidate, 'package.json')) ||
        fs.existsSync(path.join(subCandidate, 'src')) ||
        fs.existsSync(path.join(subCandidate, 'server.js')) ||
        fs.existsSync(path.join(subCandidate, 'app.js')) ||
        fs.existsSync(path.join(subCandidate, 'index.html')) ||
        fs.existsSync(path.join(subCandidate, 'main.py'))
      );
    });

    // Only narrow if there is exactly ONE candidate subfolder (e.g. repo/client or repo/frontend only).
    // If there are multiple (e.g. backend + frontend), preserve the root so both are evaluated!
    if (subprojects.length === 1) {
      const targetSub = path.join(repoPath, subprojects[0].name);
      logger.info(`Auto-detected single project subfolder inside monorepo: ${subprojects[0].name}`);
      return targetSub;
    }
  }

  return repoPath;
}

/**
 * Deletes a cloned repository safely, cleaning up the entire clone root even if a subfolder was evaluated.
 */
export async function deleteRepo(targetPath) {
  try {
    if (!targetPath) return;

    // If targetPath is inside TEMP_DIR, delete the top-level repo folder inside TEMP_DIR
    const rel = path.relative(TEMP_DIR, targetPath);
    if (!rel.startsWith('..') && !path.isAbsolute(rel)) {
      const topFolderName = rel.split(path.sep)[0];
      const topLevelFolder = path.join(TEMP_DIR, topFolderName);
      fs.rmSync(topLevelFolder, { recursive: true, force: true });
      return;
    }

    // If inside system temp dir (e.g. IDE materialization sandbox)
    const relTmp = path.relative(os.tmpdir(), targetPath);
    if (!relTmp.startsWith('..') && !path.isAbsolute(relTmp)) {
      fs.rmSync(targetPath, { recursive: true, force: true });
      return;
    }

    // Do NOT delete external local directories
    logger.info(`Preserving external local directory: ${targetPath}`);
  } catch (err) {
    logger.error(`[REPO CLEANUP ERROR] ${err.message}`);
  }
}