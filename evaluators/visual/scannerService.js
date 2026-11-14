import { globby } from 'globby';
import path from 'path';

/**
 * Deterministically choose the entry HTML file (V-20):
 *   1. an explicit `entryFile` (basename or suffix match), if provided;
 *   2. otherwise prefer `index.html`, then the shallowest file, then
 *      lexicographic order — never just "whatever globby returned first".
 */
export function pickEntryHtml(htmlFiles, rootDir, entryFile) {
  if (htmlFiles.length === 0) return null;

  if (entryFile) {
    const match = htmlFiles.find(
      f => path.basename(f) === entryFile || f.endsWith(entryFile)
    );
    if (match) return match;
  }

  const ranked = htmlFiles
    .map(f => {
      const base = path.basename(f).toLowerCase();
      let isIndex = 2;
      if (base === 'index.html') isIndex = 0;
      else if (base === 'home.html') isIndex = 1;
      return {
        f,
        isIndex,
        depth: path.relative(rootDir, f).split(path.sep).length
      };
    })
    .sort(
      (a, b) =>
        a.isIndex - b.isIndex ||
        a.depth - b.depth ||
        a.f.localeCompare(b.f)
    );

  return ranked[0].f;
}

export async function scanStudentFolders(rootDir, entryFile = null) {

  const ignorePatterns = ['!**/node_modules/**', '!**/.git/**', '!**/dist/**', '!**/build/**'];
  const htmlFiles = await globby(['**/*.html', ...ignorePatterns], { cwd: rootDir, absolute: true });
  const cssFiles = await globby(['**/*.css', ...ignorePatterns], { cwd: rootDir, absolute: true });
  const jsFiles = await globby(['**/*.js', ...ignorePatterns], { cwd: rootDir, absolute: true });

  // If no HTML files found directly, check if the student committed a zip archive into their repo
  if (htmlFiles.length === 0) {
    const zipFiles = await globby(['**/*.zip', ...ignorePatterns], { cwd: rootDir, absolute: true });
    if (zipFiles.length > 0) {
      try {
        const { exec } = await import('child_process');
        const util = await import('util');
        const execPromise = util.promisify(exec);
        const pythonBin = process.platform === 'win32' ? 'python' : 'python3';
        const fileURLToPath = (await import('url')).fileURLToPath;
        const currentDir = path.dirname(fileURLToPath(import.meta.url));
        const safeUnzip = path.resolve(currentDir, '../react/utils/safe_unzip.py');
        for (const zip of zipFiles) {
          await execPromise(`"${pythonBin}" "${safeUnzip}" "${zip}" "${path.dirname(zip)}"`, { timeout: 30000 }).catch(() => {});
        }
        const recheckHtml = await globby(['**/*.html', ...ignorePatterns], { cwd: rootDir, absolute: true });
        if (recheckHtml.length > 0) {
          htmlFiles.push(...recheckHtml);
          const recheckCss = await globby(['**/*.css', ...ignorePatterns], { cwd: rootDir, absolute: true });
          cssFiles.push(...recheckCss);
          const recheckJs = await globby(['**/*.js', ...ignorePatterns], { cwd: rootDir, absolute: true });
          jsFiles.push(...recheckJs);
        }
      } catch {}
    }
  }

  const flags = [];
  if (htmlFiles.length === 0) flags.push('Missing HTML');

  return {
    html: pickEntryHtml(htmlFiles, rootDir, entryFile),
    htmlFiles,
    css: cssFiles,
    js: jsFiles,
    flags,
    basePath: rootDir
  };
}
