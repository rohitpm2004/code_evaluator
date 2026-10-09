import { globby } from 'globby';
import path from 'path';

/**
 * Deterministically choose the entry HTML file (V-20):
 *   1. an explicit `entryFile` (basename or suffix match), if provided;
 *   2. otherwise prefer `index.html`, then the shallowest file, then
 *      lexicographic order — never just "whatever globby returned first".
 */
function pickEntryHtml(htmlFiles, rootDir, entryFile) {
  if (htmlFiles.length === 0) return null;

  if (entryFile) {
    const match = htmlFiles.find(
      f => path.basename(f) === entryFile || f.endsWith(entryFile)
    );
    if (match) return match;
  }

  const ranked = htmlFiles
    .map(f => ({
      f,
      isIndex: path.basename(f).toLowerCase() === 'index.html' ? 0 : 1,
      depth: path.relative(rootDir, f).split(path.sep).length
    }))
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

  const flags = [];
  if (htmlFiles.length === 0) flags.push('Missing HTML');
  if (cssFiles.length === 0) flags.push('Missing CSS');

  return {
    html: pickEntryHtml(htmlFiles, rootDir, entryFile),
    css: cssFiles,
    js: jsFiles,
    flags,
    basePath: rootDir
  };
}
