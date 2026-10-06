/**
 * Which harness stack this process drives.
 *
 * The suite can run on several identical, independent stacks at once (see
 * stacks.sh and README.md): stack 0 is the original one (compose project
 * nibe-e2e, ports 18123/18830/18443), stack i > 0 is project nibe-e2e-<b|c|…>
 * on ports i·10000 higher, with its own Home Assistant, broker, mock API,
 * bridge and seed-out directory. Playwright runs one worker per stack and
 * gives each worker a stable TEST_PARALLEL_INDEX, which selects its stack;
 * E2E_STACK forces one (a single-stack run against stack B, say).
 *
 * Importing this module points the process at its stack by setting the
 * environment variables everything else already reads — HA_URL,
 * MOCK_API_URL, the container names, and COMPOSE_PROJECT_NAME plus the port
 * and name variables docker-compose.yml interpolates, so a plain
 * `docker compose …` from a spec acts on its own stack. Specs therefore
 * keep their `process.env.X || default` constants; the support modules
 * import this first, so it runs before any spec's module body.
 */

export interface Stack {
  index: number;
  project: string;
  haPort: number;
  mqttPort: number;
  mockPort: number;
  seedOut: string; // relative to dev/e2e
}

const LETTERS = 'abcdefgh';

export function stackFor(index: number): Stack {
  if (index === 0) {
    return { index, project: 'nibe-e2e', haPort: 18123, mqttPort: 18830, mockPort: 18443, seedOut: 'seed-out' };
  }
  const letter = LETTERS[index];
  const offset = index * 10000;
  return {
    index,
    project: `nibe-e2e-${letter}`,
    haPort: 18123 + offset,
    mqttPort: 18830 + offset,
    mockPort: 18443 + offset,
    seedOut: `seed-out-${letter}`,
  };
}

export function currentStackIndex(): number {
  const forced = process.env.E2E_STACK;
  if (forced !== undefined && forced !== '') return Number(forced);
  return Number(process.env.TEST_PARALLEL_INDEX ?? 0);
}

/** The environment that points a process (or a docker compose call) at a stack. */
export function stackEnv(stack: Stack): Record<string, string> {
  return {
    HA_URL: `http://localhost:${stack.haPort}`,
    MOCK_API_URL: `https://localhost:${stack.mockPort}`,
    BRIDGE_CONTAINER: `${stack.project}-bridge`,
    HA_CONTAINER: `${stack.project}-homeassistant`,
    MQTT_CONTAINER: `${stack.project}-mosquitto-1`,
    MOCK_API_CONTAINER: `${stack.project}-mock-api`,
    COMPOSE_PROJECT_NAME: stack.project,
    E2E_PREFIX: stack.project,
    HA_PORT: String(stack.haPort),
    MQTT_PORT: String(stack.mqttPort),
    MOCK_PORT: String(stack.mockPort),
    SEED_OUT: `./${stack.seedOut}`,
  };
}

export const STACK = stackFor(currentStackIndex());
Object.assign(process.env, stackEnv(STACK));
