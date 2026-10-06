import { test, expect } from '@playwright/test';
import { execFileSync } from 'child_process';
import { BRIDGE_CONTAINER, DEFAULT_BRIDGE_ENV, MQTT_CONTAINER, recreateBridge } from './support/stack';

/**
 * The debug-mode "Run Test Suite" button runs pytest with --dist=loadscope.
 *
 * Several test classes share one /tmp path; under xdist's default
 * distribution two of their tests can land on different workers and race on
 * the file — the project's own measurement was a failure on 3 of 4 runs.
 * The documented command passes --dist=loadscope; the in-container runner
 * didn't, so the button reported "tests failed" for a healthy bridge.
 *
 * Starts a run through the button's MQTT press topic and reads the running
 * pytest command line inside the bridge container; recreating the bridge
 * afterwards aborts the run.
 */

function pytestCommandLine(): string {
  try {
    const ps = execFileSync('docker', ['exec', BRIDGE_CONTAINER, 'ps', '-o', 'args'], {
      encoding: 'utf-8',
    });
    return ps.split('\n').find((l) => l.includes('-m pytest')) ?? '';
  } catch {
    return '';
  }
}

test('the Run Test Suite button keeps each test class on one xdist worker', async () => {
  test.setTimeout(240_000);
  try {
    await recreateBridge({ BRIDGE_OPTIONS: './bridge/options-debug.json', BRIDGE_SUPERVISOR_TOKEN: '' });
    // What HA's button sends: a plain, non-retained publish.
    execFileSync('docker', [
      'exec', MQTT_CONTAINER, 'mosquitto_pub', '-t', 'homeassistant/button/nibe_run_tests/press', '-m', 'PRESS',
    ]);
    let cmd = '';
    await expect
      .poll(() => (cmd = pytestCommandLine()), {
        timeout: 60_000,
        message: 'precondition: no pytest process started in the bridge container',
      })
      .not.toBe('');
    expect(cmd, 'pytest runs without --dist=loadscope').toContain('--dist=loadscope');
  } finally {
    await recreateBridge(DEFAULT_BRIDGE_ENV);
  }
});
