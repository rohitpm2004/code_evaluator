import path from 'path';
import fs from 'fs';
import os from 'os';
import { simpleGit } from 'simple-git';
import { assertSafeUrl, getAllowedGitHosts } from '../visual/utils/urlGuard.js';
import { parseGithubTreeUrl } from '../backend/extractService.js';
import logger from '../../config/logger.js';

const TEMP_DIR = path.join(process.cwd(), 'temp');

/**
 * Clones a repository or prepares a local project directory.
 * Supports:
 * 1. Standard Git repository URLs
 * 2. GitHub tree URLs with subfolders (e.g. https://github.com/user/repo/tree/main/client)
 * 3. Already-materialized local directories (e.g. in-browser IDE submissions)
 * 4. Automatic monorepo subfolder detection if package.json is in a subfolder
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

  // 2. Parse GitHub tree / subfolder URLs
  const parsedGithub = parseGithubTreeUrl(repoUrl);
  const actualCloneUrl = parsedGithub ? parsedGithub.cloneUrl : repoUrl;

  await assertSafeUrl(actualCloneUrl, { allowedHosts: getAllowedGitHosts() });

  if (!fs.existsSync(TEMP_DIR)) {
    fs.mkdirSync(TEMP_DIR, { recursive: true });
  }

  const { randomUUID } = await import('crypto');
  const repoName = `repo_${Date.now()}_${randomUUID()}`;
  const repoPath = path.join(TEMP_DIR, repoName);

  const cloneArgs = ['--depth', '1'];
  if (parsedGithub && parsedGithub.branch) {
    cloneArgs.push('--branch', parsedGithub.branch);
  }
  cloneArgs.push('--');

  await simpleGit({ env: { ...process.env, GIT_TERMINAL_PROMPT: '0' } }).clone(actualCloneUrl, repoPath, cloneArgs);

  // 3. Resolve explicit subpath if parsed from a GitHub tree URL
  if (parsedGithub && parsedGithub.subPath) {
    const explicitSubPath = path.resolve(repoPath, parsedGithub.subPath);
    if (explicitSubPath.startsWith(repoPath) && fs.existsSync(explicitSubPath)) {
      logger.info(`Resolved explicit subfolder from GitHub URL: ${parsedGithub.subPath}`);
      return explicitSubPath;
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
        fs.existsSync(path.join(subCandidate, 'app.js'))
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