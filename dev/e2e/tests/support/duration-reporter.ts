import type { FullResult, Reporter, TestCase, TestResult } from '@playwright/test/reporter';
import * as fs from 'fs';
import * as path from 'path';
import { DURATIONS_FILE, readDurations } from './spec-order';

/**
 * Records how long each spec file took (all its tests, retries included) in
 * test-durations.json, which spec-order.ts uses to start the longest specs
 * first. Merged, so a run of a few specs only updates those. Also prints the
 * slowest files at the end of a run.
 */
export default class DurationReporter implements Reporter {
  private perFile: Record<string, number> = {};

  onTestEnd(test: TestCase, result: TestResult): void {
    const file = path.basename(test.location.file);
    this.perFile[file] = (this.perFile[file] ?? 0) + result.duration;
  }

  onEnd(_result: FullResult): void {
    if (Object.keys(this.perFile).length === 0) return;
    const merged = { ...readDurations(), ...this.perFile };
    const sorted = Object.fromEntries(Object.entries(merged).sort(([a], [b]) => a.localeCompare(b)));
    fs.writeFileSync(DURATIONS_FILE, JSON.stringify(sorted, null, 2) + '\n');
    const slowest = Object.entries(this.perFile)
      .sort(([, a], [, b]) => b - a)
      .slice(0, 8)
      .map(([f, ms]) => `  ${(ms / 1000).toFixed(0).padStart(5)}s  ${f}`);
    console.log(`\nSlowest spec files this run:\n${slowest.join('\n')}`);
  }

  printsToStdio(): boolean {
    return false;
  }
}
