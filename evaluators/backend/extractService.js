import path from "path";
import os from "os";
import fs from "fs-extra";
import crypto from "crypto";
import simpleGit from "simple-git";
import { assertSafeUrl, getAllowedGitHosts } from "../visual/utils/urlGuard.js";

const TMP_ROOT = path.join(os.tmpdir(), "backend-evaluator");

await fs.ensureDir(TMP_ROOT);

// Author: Arma Sahar
// Bug: reported with a screenshot from the live UI — a repoUrl like
// "https://github.com/user/repo/tree/main/HTTP%20server/Assignment" always
// failed with a generic "couldn't download the repository" error. That's
// GitHub's own web-UI URL for *browsing a folder in a browser* — the format
// `git clone` understands is "https://github.com/user/repo.git" (confirmed:
// `git ls-remote` on the tree URL fails with "repository not found", the
// same URL with the /tree/... suffix stripped clones fine). This is a very
// common real submission shape: a student's assignment lives in a subfolder
// of a larger class repo, and copying the URL from GitHub's folder view
// (rather than constructing the repo's clone URL by hand) is the natural
// thing to do. Fix: detect this specific GitHub shape, clone the actual
// repo at the referenced branch, and root the evaluation at the referenced
// subfolder instead of the repo root — everything downstream (sandbox
// upload, detectLanguage, the test runners) is unaffected, since they only
// ever see whatever path extractSubmission returns.
const GITHUB_TREE_URL_RE = /^https?:\/\/github\.com\/([^/]+)\/([^/]+)\/tree\/([^/]+)(?:\/(.*))?\/?$/i;

// Author: Arma Sahar
// Bug: reported live -- "https://github.com/user/repo/blob/main" (no file
// segment at all, a truncated/malformed paste) failed with a generic clone
// error. GitHub's "blob" URL is its web-UI page for viewing a *single
// file*, not a clone target, same underlying problem as the /tree/ case
// above but never handled. Also covers the more common shape with a real
// file, e.g. ".../blob/main/src/index.js" -- rooting the evaluation at the
// file itself would break every evaluator (they expect a project
// directory: package.json, entry point, etc.), so this roots at the file's
// *containing folder* instead, same as a /tree/ URL pointed at that folder.
const GITHUB_BLOB_URL_RE = /^https?:\/\/github\.com\/([^/]+)\/([^/]+)\/blob\/([^/]+)(?:\/(.*))?\/?$/i;

function buildGithubRef(owner, repo, branch, subPath) {
  return {
    cloneUrl: `https://github.com/${owner}/${repo.replace(/\.git$/i, "")}.git`,
    branch: decodeURIComponent(branch),
    subPath: subPath || null
  };
}

export function parseGithubTreeUrl(url) {
  const trimmed = url.trim();

  const treeMatch = GITHUB_TREE_URL_RE.exec(trimmed);
  if (treeMatch) {
    const [, owner, repo, branch, rawPath] = treeMatch;
    const subPath = rawPath
      ? rawPath.split("/").map(decodeURIComponent).join(path.sep)
      : null;
    return buildGithubRef(owner, repo, branch, subPath);
  }

  const blobMatch = GITHUB_BLOB_URL_RE.exec(trimmed);
  if (blobMatch) {
    const [, owner, repo, branch, rawPath] = blobMatch;
    if (!rawPath) return buildGithubRef(owner, repo, branch, null);

    const decodedPath = rawPath.split("/").map(decodeURIComponent).join(path.sep);
    const dir = path.dirname(decodedPath);
    return buildGithubRef(owner, repo, branch, dir === "." ? null : dir);
  }

  // Direct file URL fallback: e.g. https://github.com/owner/repo/something.js
  const directFileMatch = /^https?:\/\/github\.com\/([^/]+)\/([^/]+)\/(.+\.[a-z0-9]+)$/i.exec(trimmed);
  if (directFileMatch) {
    const [, owner, repo, filePath] = directFileMatch;
    const dir = path.dirname(filePath);
    return buildGithubRef(owner, repo, null, dir === "." ? null : dir);
  }

  return null;
}

// Returns { uploadPath, cleanupPath }: `uploadPath` is what gets uploaded
// to the sandbox (may be a subfolder of the clone, for a GitHub tree URL);
// `cleanupPath` is always the top-level clone directory, so cleanup removes
// the *whole* checkout even when only a subfolder of it was graded — a
// tree-URL submission must not leak the rest of the cloned repo on disk.
export default async function extractSubmission(repoUrl) {

  if (!repoUrl) {
    throw new Error("Repository URL is required");
  }

  const treeUrl = parseGithubTreeUrl(repoUrl);
  const cloneUrl = treeUrl ? treeUrl.cloneUrl : repoUrl;

  // Bug (backendBugs.md #7): unlike the visual/JS evaluators, `repoUrl` here
  // went straight into `git clone` with no SSRF/allowlist check — a
  // submission could point at an internal host, a loopback/metadata address,
  // or (without a `--` terminator) an argument that git interprets as a
  // flag instead of a URL. Same guard the visual and JS evaluators use.
  // (Validated against the *rewritten* clone URL when a tree URL was
  // detected — same host either way, so this doesn't relax the check.)
  await assertSafeUrl(cloneUrl, { allowedHosts: getAllowedGitHosts() });

  const folderName =
    `submission-${Date.now()}-${crypto.randomBytes(4).toString("hex")}`;

  const targetPath = path.join(TMP_ROOT, folderName);

  await fs.ensureDir(targetPath);

  // Bug: reported from the deployed instance right after the E2B fix landed
  // -- a clone of a genuinely public repo failed with "fatal: could not
  // read Username for 'https://github.com': No such device or address".
  // That's git trying to open a TTY to prompt for credentials and failing
  // because there isn't one on a headless server -- happens for a repo
  // that's actually private/mistyped/rate-limited, where a local dev
  // machine's git would just prompt (and eventually the request would still
  // fail, only slower). GIT_TERMINAL_PROMPT=0 makes git fail fast with a
  // real error message instead of attempting to prompt at all.
  const git = simpleGit().env("GIT_TERMINAL_PROMPT", "0");

  try {

    const cloneArgs = ["--depth", "1"];
    if (treeUrl) {
      cloneArgs.push("--branch", treeUrl.branch);
    }
    cloneArgs.push("--");

    await git.clone(cloneUrl, targetPath, cloneArgs);

    if (!treeUrl || !treeUrl.subPath) {
      return { uploadPath: targetPath, cleanupPath: targetPath };
    }

    // Guard against a malicious/malformed subPath (e.g. "../../etc") ever
    // resolving outside the freshly-cloned targetPath.
    const subPath = path.resolve(targetPath, treeUrl.subPath);
    if (!subPath.startsWith(targetPath + path.sep)) {
      throw new Error(`Invalid path in repository URL: "${treeUrl.subPath}"`);
    }
    if (!(await fs.pathExists(subPath))) {
      throw new Error(
        `Path "${treeUrl.subPath}" was not found in the repository (branch "${treeUrl.branch}").`
      );
    }

    return { uploadPath: subPath, cleanupPath: targetPath };

  } catch (err) {

    await fs.remove(targetPath);

    throw new Error(
      `Failed to clone repository: ${err.message}`
    );
  }
}