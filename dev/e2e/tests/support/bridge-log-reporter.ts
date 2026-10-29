import type { FullResult, Reporter, TestCase, TestResult } from '@playwright/test/reporter';
import { ChildProcess, execSync, spawn } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';
import { stackEnv, stackFor } from './stacks';

export const BRIDGE_LOG_DIR = path.join(__dirname, '..', '..', 'bridge-logs');

/**
 * Keeps every stack's bridge log for the whole run, in
 * bridge-logs/<container>.log.
 *
 * Several specs restart or recreate the bridge container, and `docker logs`
 * only shows what the current container wrote — so a spec that failed early
 * in a run could leave no log to look at by the end of it. Each stack gets a
 * collector that fetches the new lines every two seconds, up to three seconds
 * ago so a clock difference between this host and the Docker VM can't skip
 * any. It doesn't follow (`docker logs -f`): through Colima such a stream can
 * stall without ending, and stopped the log mid-run. The last seconds before
 * a container is recreated can be missing. A failed test names the log of
 * its stack. The run before keeps its log as <container>.prev.log.
 */
export default class BridgeLogReporter implements Reporter {
  private followers: ChildProcess[] = [];
  private files = new Map<number, string>();
  private containers = new Map<string, string>(); // log file → container

  onBegin(): void {
    fs.mkdirSync(BRIDGE_LOG_DIR, { recursive: true });
    const since = new Date().toISOString().replace(/\.\d+Z$/, 'Z');
    for (const index of stacksInRun()) {
      const container = stackEnv(stackFor(index)).BRIDGE_CONTAINER;
      const file = path.join(BRIDGE_LOG_DIR, `${container}.log`);
      // The previous run's log is kept once more: rerunning a failed spec
      // on its own would otherwise wipe the log of the run it failed in.
      if (fs.existsSync(file)) fs.renameSync(file, path.join(BRIDGE_LOG_DIR, `${container}.prev.log`));
      fs.writeFileSync(file, '');
      this.files.set(index, file);
      this.containers.set(file, container);
      const loop = [
        `since=${since}`,
        'while :; do',
        '  until=$(date -u -v-3S +%Y-%m-%dT%H:%M:%SZ)',
        `  if docker inspect ${container} >/dev/null 2>&1; then`,
        `    docker logs --since "$since" --until "$until" ${container} >> "${file}" 2>&1 && since=$until && echo "$since" > "${file}.since"`,
        '  fi',
        '  sleep 2',
        'done',
      ].join('\n');
      // Its own process group, so stopping it also stops its `docker logs`.
      this.followers.push(spawn('bash', ['-c', loop], { stdio: 'ignore', detached: true }));
    }
    process.once('exit', () => this.stop());
  }

  onTestEnd(test: TestCase, result: TestResult): void {
    if (result.status === 'passed' || result.status === 'skipped') return;
    const forced = process.env.E2E_STACK;
    const index = forced ? Number(forced) : result.parallelIndex;
    const file = this.files.get(index);
    if (file) console.log(`  bridge log for ${test.title}: ${path.relative(process.cwd(), file)}`);
  }

  onEnd(_result: FullResult): void {
    this.stop();
  }

  printsToStdio(): boolean {
    return false;
  }

  private stop(): void {
    if (this.followers.length === 0) return;
    for (const follower of this.followers) {
      try {
        if (follower.pid) process.kill(-follower.pid, 'SIGTERM');
      } catch {
        // already gone
      }
    }
    this.followers = [];
    // The collectors stay a few seconds behind; fetch what they hadn't yet.
    for (const [file, container] of this.containers) {
      const sinceFile = `${file}.since`;
      try {
        if (fs.existsSync(sinceFile)) {
          const since = fs.readFileSync(sinceFile, 'utf-8').trim();
          execSync(`docker logs --since ${since} ${container} >> "${file}" 2>&1`, { stdio: 'ignore' });
          fs.rmSync(sinceFile);
        }
      } catch {
        // container gone; keep what was collected
      }
    }
  }
}

function stacksInRun(): number[] {
  const forced = process.env.E2E_STACK;
  if (forced) return [Number(forced)];
  return [...Array(Number(process.env.E2E_STACKS || 1)).keys()];
}
