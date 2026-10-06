import { execFileSync } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';

/**
 * The order the spec files run in: what's being worked on first, so a broken
 * new or changed spec fails in the first minutes of a run rather than the
 * last, then the rest longest first, so the stacks running in parallel
 * finish at about the same time instead of one picking up a 7-minute spec
 * at the end.
 *
 * - Specs git reports as modified or untracked, most recently edited first.
 * - Then every other spec by its last recorded duration (DURATIONS_FILE,
 *   written by duration-reporter.ts), longest first; specs with no
 *   recorded duration yet go by last commit, newest first.
 *
 * E2E_ORDER=alpha restores plain alphabetical order.
 */

export const DURATIONS_FILE = path.join(__dirname, '..', '..', 'test-durations.json');

export function readDurations(): Record<string, number> {
  try {
    return JSON.parse(fs.readFileSync(DURATIONS_FILE, 'utf-8'));
  } catch {
    return {};
  }
}

function git(args: string[], cwd: string): string {
  try {
    return execFileSync('git', args, { cwd, encoding: 'utf-8', stdio: ['ignore', 'pipe', 'ignore'] });
  } catch {
    return '';
  }
}

export function orderedSpecFiles(testDir: string): string[] {
  const files = fs.readdirSync(testDir).filter((f) => f.endsWith('.spec.ts')).sort();
  if (process.env.E2E_ORDER === 'alpha') return files;

  const dirty = new Set(
    git(['status', '--porcelain', '--untracked-files=all', '--', '.'], testDir)
      .split('\n')
      .map((line) => path.basename(line.slice(3).trim()))
      .filter((f) => f.endsWith('.spec.ts'))
  );
  const mtime = (f: string) => fs.statSync(path.join(testDir, f)).mtimeMs;
  const commitTime = (f: string) => Number(git(['log', '-1', '--format=%ct', '--', f], testDir).trim() || 0);
  const durations = readDurations();

  const changed = files.filter((f) => dirty.has(f)).sort((a, b) => mtime(b) - mtime(a));
  const rest = files.filter((f) => !dirty.has(f));
  const timed = rest.filter((f) => durations[f] !== undefined).sort((a, b) => durations[b] - durations[a]);
  const untimed = rest.filter((f) => durations[f] === undefined).sort((a, b) => commitTime(b) - commitTime(a));
  return [...changed, ...timed, ...untimed];
}
