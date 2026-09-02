import { describe, it, expect } from 'vitest';
import { createCard } from './support/create-card.js';

// Every inbound topic the card actually subscribes to (see setupMqttSubscriptions()
// in nibe-entity-manager-card.js). dynamic_point_map / active_dynamic_points /
// bridge/status / bridge/alert are documented in docs/card-api.md but the card
// does not subscribe to any of them — see final report.
//
// nibe/browser/changelog/history used to be excluded from this sweep because
// _decompressPayload()'s `writer.write(bytes); writer.close();` were
// fire-and-forget, producing an unhandled promise rejection on the write side
// independently of the (correctly caught) read-side error — see
// changelog.test.js for the dedicated coverage that predates the fix. Now
// that both calls are `.catch()`-guarded, it's safe to include here too.
const TOPICS = [
  'nibe/browser/all_metadata',
  'nibe/browser/meta/1234',
  'nibe/browser/enabled_state',
  'nibe/browser/dynamic',
  'nibe/browser/changelog/history',
  'nibe/browser/changelog/unread',
  'nibe/browser/snapshots',
  'nibe/browser/applied_mode',
  'nibe/browser/point_list',
];

const GARBAGE_PAYLOADS = [
  '',
  '   ',
  'not json at all {{{',
  '42',
  'null',
  'true',
  '[]',
  '{}',
  '{"unexpected": "shape"}',
  JSON.stringify({ metadata: null, enabled_points: null, points: null, history: null }),
  null,
  undefined,
];

describe('every subscribed handler survives garbage input without throwing', () => {
  for (const topic of TOPICS) {
    for (const payload of GARBAGE_PAYLOADS) {
      it(`${topic} <- ${JSON.stringify(payload)}`, () => {
        const { harness } = createCard();
        expect(() => harness.publish(topic, payload)).not.toThrow();
      });
    }
  }
});
