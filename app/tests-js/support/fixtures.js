import { gzipSync } from 'node:zlib';

/** Gzip-compress a JS object as the bridge does for changelog/history and
 * dynamic_point_map: JSON, gzipped, base64, prefixed with the "gzip1:" sentinel. */
export function gzipPayload(obj) {
  const json = JSON.stringify(obj);
  const gz = gzipSync(Buffer.from(json, 'utf-8'));
  return 'gzip1:' + gz.toString('base64');
}

export function sampleMetadataEntry(overrides = {}) {
  return {
    id: 3945,
    title: 'Heating setpoint',
    type: 'number',
    writable: true,
    unit: '°C',
    unit_overridden: false,
    unit_raw: '°C',
    min_value: 5,
    max_value: 30,
    category: '',
    description: 'Desired room temperature when room sensor is active.',
    is_dynamic: false,
    modbusRegisterID: 40123,
    variableType: 'INT_S16',
    variableSize: 2,
    modbusRegisterType: 'MODBUS_HOLDING_REGISTER',
    shortUnit: '°C',
    divisor: 10,
    decimal: 1,
    change: 1,
    ...overrides,
  };
}

export function allMetadataPayload(entries) {
  const metadata = {};
  entries.forEach((e) => {
    metadata[String(e.id)] = e;
  });
  return JSON.stringify({ metadata, count: entries.length, last_updated: Date.now() / 1000 });
}

export function enabledStatePayload(ids) {
  return JSON.stringify({ enabled_points: ids, count: ids.length, timestamp: Date.now() / 1000 });
}

export function pointListPayload(ids) {
  return JSON.stringify({ points: ids, count: ids.length, last_updated: Date.now() / 1000 });
}

export function dynamicChangePayload(overrides = {}) {
  return JSON.stringify({
    added: [],
    removed: [],
    source: 'firmware',
    triggered_by: null,
    ...overrides,
  });
}

export function changelogHistoryPayload({
  history = [],
  total_entries = 50,
  unread_count = 0,
  seq = 1,
} = {}) {
  return gzipPayload({
    history,
    total_entries,
    unread_count,
    last_updated: Date.now() / 1000,
    _seq: seq,
  });
}

export function changelogEntry(overrides = {}) {
  return {
    timestamp: 1721825000.0,
    iso_timestamp: '2025-07-24 14:03:20',
    added: [{ id: 3755, title: 'Extra pump speed', type: 'sensor' }],
    removed: [],
    id: 'change_1721825000000',
    unread: true,
    source: 'firmware',
    triggered_by: { id: 3754, title: 'Forced control' },
    ...overrides,
  };
}

export function changelogUnreadPayload(count) {
  return JSON.stringify({ unread_count: count, last_change: Date.now() / 1000 });
}

export function snapshotsPayload(snaps) {
  return JSON.stringify(snaps);
}

export function sampleSnapshot(overrides = {}) {
  return {
    name: 'Summer Profile',
    timestamp: '2025-07-24 14:03:20',
    point_ids: [3945, 5079, 3671],
    point_count: 3,
    mode: 'essential',
    ...overrides,
  };
}
