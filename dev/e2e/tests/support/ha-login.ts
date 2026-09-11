import { type Page } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';

/**
 * Logging into the real Home Assistant frontend, in one place.
 *
 * Every spec here starts by logging in, and each one used to carry its own
 * copy of the same five lines. When Home Assistant's login form changed —
 * the username and password fields lost their accessible labels, so
 * `getByLabel('Username')` and `getByRole('textbox', { name: 'Password' })`
 * both stopped resolving — that was nine identical edits, and every spec in
 * the suite failed at once with a locator timeout that said nothing about the
 * real cause. Keeping it here makes the next HA release a one-file fix.
 *
 * The fields are addressed by their form `name` attributes, which have
 * survived every HA revision this harness has been through, rather than by
 * label text or placement.
 */

const SEED_OUT = path.join(__dirname, '..', '..', 'seed-out');

export function readCredentials(): { username: string; password: string } {
  const raw = fs.readFileSync(path.join(SEED_OUT, 'credentials.json'), 'utf-8');
  return JSON.parse(raw);
}

export function readToken(): string {
  return fs.readFileSync(path.join(SEED_OUT, 'token.txt'), 'utf-8').trim();
}

export async function loginToHa(page: Page): Promise<void> {
  const { username, password } = readCredentials();

  // Retried as a whole, because the login page is a web component that can
  // re-render after hydration and drop a value that was typed into it a
  // moment too early. Observed as an intermittent hang on
  // /auth/authorize with both fields apparently filled — and with thirteen
  // specs each opening with this, a one-in-ten flake here is a suite that
  // fails somewhere new every run.
  let lastFailure = '';
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      await page.goto('/');

      const usernameField = page.locator('input[name="username"]');
      const passwordField = page.locator('input[name="password"]');
      await usernameField.waitFor({ state: 'visible', timeout: 30_000 });

      await usernameField.fill(username);
      await passwordField.fill(password);

      // Confirm the component actually kept what was typed before submitting.
      if (
        (await usernameField.inputValue()) !== username ||
        (await passwordField.inputValue()) !== password
      ) {
        lastFailure = 'the login form did not retain the typed credentials';
        continue;
      }

      // The submit control is a custom element, so its text lives in shadow
      // DOM and only the accessibility tree sees it. Pressing Enter in the
      // password field is the fallback if a future release stops exposing it
      // as a button.
      const loginButton = page.getByRole('button', { name: /log in/i });
      if ((await loginButton.count()) > 0) {
        await loginButton.first().click();
      } else {
        await passwordField.press('Enter');
      }

      // Asserted as "off the auth pages" rather than against a list of known
      // landing paths: the default dashboard has moved before (HA 2026.9
      // lands on /home/overview, older releases on /lovelace/0) and pinning
      // the pattern to whichever one is current makes every spec fail on the
      // next rename.
      await page.waitForURL((url) => !url.pathname.startsWith('/auth/'), {
        timeout: 30_000,
      });
      return;
    } catch (error) {
      // Deliberately catching navigation and locator failures too, not just a
      // stuck URL. Under Colima the published port forwards are re-synced
      // whenever a container starts or stops, and a spec that stops one (the
      // mock API, to simulate an unreachable controller) can leave the *next*
      // spec's very first page.goto failing with net::ERR_EMPTY_RESPONSE or a
      // socket hang up a few hundred milliseconds in. Two specs died that way
      // on every run, immediately after the one that restarts the mock API,
      // while the spec after them passed — which looks like two broken tests
      // rather than one flaky port forward.
      lastFailure = error instanceof Error ? error.message.split('\n')[0] : String(error);
      if (attempt < 3) {
        await page.waitForTimeout(5_000);
      }
    }
  }

  throw new Error(`could not log into Home Assistant after 3 attempts: ${lastFailure}`);
}
