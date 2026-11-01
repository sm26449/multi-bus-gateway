// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
//
// The device page's one write path (ui/js/device-store.js, ADR-0030), with
// no browser: which tag a write sends, which answer's tag it keeps, what a
// 412 tells the page, and the "what changed" diff.
//
//   node tools/e2e/device_store_model_test.mjs
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const M = require('../../ui/js/device-store.js');

let failed = 0, n = 0;
const check = (name, cond, extra = '') => {
  n++; if (!cond) failed++;
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra !== '' ? ' — ' + JSON.stringify(extra) : ''}`);
};
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// a fake gateway: answers what the test queues, records what it was sent
function gateway() {
  const sent = [], queue = [];
  const fetch = async (path, init = {}) => {
    sent.push({ path, method: init.method || 'GET', headers: { ...(init.headers || {}) }, body: init.body });
    const [status, body, headers] = queue.shift() || [200, {}, {}];
    return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json', ...headers } });
  };
  return { sent, queue, fetch, last: () => sent[sent.length - 1] };
}
const checked = tag => ({ ETag: tag, 'MBG-If-Match': 'checked' });
const ignored = tag => ({ ETag: tag, 'MBG-If-Match': 'ignored' });

// opening: GET one device holds its entry and its tag
{
  const g = gateway();
  const s = M.createStore({ fetch: g.fetch });
  g.queue.push([200, { device: { id: 'em2', name: 'Meter' } }, { ETag: '"t1"' }]);
  const e = await s.load('em2');
  check('load: GET /api/devices/{id}', g.last().path === '/api/devices/em2' && g.last().method === 'GET');
  check('load: holds the entry and the tag', e.name === 'Meter' && s.id === 'em2' && s.etag === '"t1"');
  g.queue.push([404, { detail: 'device not found' }, {}]);
  check('load: a failed read answers null and keeps what it held', (await s.load('gone')) === null && s.etag === '"t1"');
}

// a write: If-Match sent, the checked answer's tag kept, the next write uses it
{
  const g = gateway();
  const s = M.createStore({ fetch: g.fetch });
  s.open('em2', { id: 'em2', name: 'Meter' }, '"t1"');
  g.queue.push([200, { ok: true }, checked('"t2"')]);
  const r = await s.devWrite('em2', 'POST', '/api/devices/em2/write-lock', { locked: false });
  check('devWrite: answers the Response', r.ok && (await r.json()).ok === true);
  check('devWrite: sends If-Match with the held tag', g.last().headers['If-Match'] === '"t1"');
  check('devWrite: JSON body and its type', g.last().body === '{"locked":false}' && g.last().headers['Content-Type'] === 'application/json');
  check('devWrite: keeps the checked answer\'s tag', s.etag === '"t2"');
  g.queue.push([200, { status: 'updated' }, checked('"t3"')]);
  await s.devWrite('em2', 'PUT', '/api/devices/em2', { id: 'em2', name: 'B' });
  check('devWrite: Save after a sub-write sends the new tag (no false 412)', g.last().headers['If-Match'] === '"t2"' && s.etag === '"t3"');
  g.queue.push([200, {}, checked('"t4"')]);
  await s.devWrite('em2', 'POST', '/api/devices/em2/restore');
  check('devWrite: no body, no Content-Type', g.last().body === undefined && !('Content-Type' in g.last().headers));
}

// what never moves the held tag
{
  const g = gateway();
  const s = M.createStore({ fetch: g.fetch });
  s.open('em2', { id: 'em2' }, '"t1"');
  g.queue.push([200, {}, ignored('"newer"')]);
  await s.devWrite('em2', 'POST', '/api/devices/em2/test');
  check('runtime answer: its (newer) tag is not taken', s.etag === '"t1"');
  g.queue.push([422, { detail: { errors: ['x'] } }, checked('"t1"')]);
  await s.devWrite('em2', 'POST', '/api/devices/em2/publishing', {});
  check('a refusal: the tag stays', s.etag === '"t1"');
  g.queue.push([200, {}, checked('"other"')]);
  await s.devWrite('inv1', 'POST', '/api/devices/inv1/outputs', { mqtt: true });
  check('another device: no If-Match sent', !('If-Match' in g.last().headers));
  check('another device: its tag is not taken', s.etag === '"t1"');
  g.queue.push([200, {}, {}]);
  await s.devWrite('em2', 'POST', '/api/devices/em2/outputs', {});
  check('an answer without a tag: the tag stays', s.etag === '"t1"');
  const empty = M.createStore({ fetch: g.fetch });
  g.queue.push([200, {}, checked('"x"')]);
  await empty.devWrite('em2', 'PUT', '/api/devices/em2', {});
  check('nothing held: no If-Match, and nothing taken', !('If-Match' in g.last().headers) && empty.etag === null);
}

// a 412: the page is told, no retry, the tag it held stays
{
  const g = gateway();
  const s = M.createStore({ fetch: g.fetch });
  const held = { id: 'em2', name: 'Meter', connection: { unit_id: 2 }, running: true };
  s.open('em2', held, '"t1"');
  const told = [];
  s.onConflict = c => told.push(c);
  const current = { id: 'em2', name: 'Garage (tab A)', connection: { unit_id: 2 }, running: false };
  g.queue.push([412, { detail: { errors: ['If-Match: the device changed'], current, etag: '"tA"' } }, checked('"tA"')]);
  const r = await s.devWrite('em2', 'PUT', '/api/devices/em2', { id: 'em2', connection: { unit_id: 5 } });
  check('412: the caller still gets the Response (and can read it)', r.status === 412 && (await r.json()).detail.etag === '"tA"');
  check('412: one request, no retry', g.sent.length === 1);
  check('412: the page is told once, with what is stored now', told.length === 1 && told[0].current.name === 'Garage (tab A)' && told[0].etag === '"tA"');
  check('412: …and what it held', told[0].held === held && told[0].path === '/api/devices/em2' && told[0].method === 'PUT');
  check('412: the held tag stays (a second Save is refused too)', s.etag === '"t1"' && s.conflict === told[0]);
  s.open('em2', current, '"tA"');
  check('open: a reload clears the conflict', s.conflict === null && s.etag === '"tA"');
}

// the diff: stored settings only, dotted paths, sorted
{
  const d = M.diff({ id: 'a', name: 'Old', connection: { unit_id: 2, host: 'h' }, running: true, meters_status: { x: 1 } },
                   { id: 'a', name: 'New', connection: { unit_id: 3, host: 'h' }, running: false, meters_status: {}, write_locked: true });
  check('diff: what changed, runtime fields left out', eq(d.map(x => x.path), ['connection.unit_id', 'name', 'write_locked']), d);
  check('diff: before and after', eq(d[1], { path: 'name', before: 'Old', after: 'New' }));
  check('diff: a list is one value', eq(M.diff({ opcua_fields: ['a'] }, { opcua_fields: ['a', 'b'] }).map(x => x.path), ['opcua_fields']));
  check('diff: nothing stored changed', M.diff({ name: 'x', running: true }, { name: 'x', running: false }).length === 0);
}

// two writes at once (two quick toggles): the second waits for the first
// and sends the tag it returned — never the same tag twice (review MAJOR 1)
{
  const sent = [];
  let tagN = 1;
  const slow = async (path, init = {}) => {
    sent.push({ path, ifMatch: (init.headers || {})['If-Match'] });
    await new Promise(r => setTimeout(r, 30));
    tagN++;
    return new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json', ...checked(`"t${tagN}"`) } });
  };
  const s = M.createStore({ fetch: slow });
  s.open('em2', { id: 'em2' }, '"t1"');
  const [a, b] = await Promise.all([
    s.devWrite('em2', 'POST', '/api/devices/em2/outputs', { mqtt: false }),
    s.devWrite('em2', 'POST', '/api/devices/em2/outputs', { influxdb: false }),
  ]);
  check('overlap: both go through', a.ok && b.ok);
  check('overlap: the second sends the tag the first returned', eq(sent.map(x => x.ifMatch), ['"t1"', '"t2"']), sent);
  check('overlap: the store ends on the last tag', s.etag === '"t3"');
  // a refused write does not stall the ones after it
  const g = gateway();
  const s2 = M.createStore({ fetch: g.fetch });
  s2.open('em2', { id: 'em2' }, '"t1"');
  g.queue.push([500, {}, {}], [200, {}, checked('"t2"')]);
  const [x, y] = await Promise.all([s2.devWrite('em2', 'PUT', '/api/devices/em2', {}), s2.devWrite('em2', 'POST', '/api/devices/em2/outputs', {})]);
  check('overlap: a failed write does not stop the next', x.status === 500 && y.ok && s2.etag === '"t2"');
}

// the held entry follows the page's own saves (review MINOR 7)
{
  const g = gateway();
  const s = M.createStore({ fetch: g.fetch });
  s.open('em2', { id: 'em2', name: 'Old' }, '"t1"');
  g.queue.push([200, { status: 'updated', device: { id: 'em2', name: 'Mine' } }, checked('"t2"')]);
  await s.devWrite('em2', 'PUT', '/api/devices/em2', { name: 'Mine' });
  check('own save: an answer carrying the entry replaces the held one', s.entry.name === 'Mine' && !s.ownSaves);
  g.queue.push([200, { ok: true }, checked('"t3"')]);
  await s.devWrite('em2', 'POST', '/api/devices/em2/write-lock', { locked: false });
  check('own save: an answer without it is noted', s.ownSaves === true);
  g.queue.push([412, { detail: { current: { id: 'em2' }, etag: '"x"' } }, checked('"x"')]);
  let told = null;
  s.onConflict = c => { told = c; };
  await s.devWrite('em2', 'POST', '/api/devices/em2/outputs', {});
  check('own save: the conflict says the list may hold the page\'s own saves', told && told.ownSaves === true && told.held.name === 'Mine');
}

// refresh: after a write outside /api/devices, the open device's tag is read again (review MAJOR 2)
{
  const g = gateway();
  const s = M.createStore({ fetch: g.fetch });
  s.open('pv-u1', { id: 'pv-u1', name: 'Old' }, '"t1"');
  g.queue.push([200, { device: { id: 'pv-u1', name: 'Renamed' } }, { ETag: '"t2"' }]);
  const e = await s.refresh('pv-u1');
  check('refresh: GET one device, the new entry and tag held', g.last().path === '/api/devices/pv-u1' && e.name === 'Renamed' && s.etag === '"t2"');
  const before = g.sent.length;
  check('refresh: another device than the open one reads nothing', (await s.refresh('other')) === null && g.sent.length === before);
  g.queue.push([500, {}, {}]);
  check('refresh: a failed read keeps the tag', (await s.refresh('pv-u1')) === null && s.etag === '"t2"');
}

// a dry run (the Edit modal's live check, L3.4): sent with the tag, it moves nothing held
{
  const g = gateway();
  const s = M.createStore({ fetch: g.fetch });
  s.open('em2', { id: 'em2', name: 'Meter' }, '"t1"');
  g.queue.push([200, { status: 'valid', would: 'update', changes: [] }, checked('"t1"')]);
  await s.devWrite('em2', 'PATCH', '/api/devices/em2?dry_run=true', { sections: { identity: { name: 'x' } } });
  check('dry run: sends If-Match (a stale version is found before Save)', g.last().headers['If-Match'] === '"t1"');
  check('dry run: never noted as an own save, the entry kept', s.ownSaves === false && s.entry.name === 'Meter' && s.etag === '"t1"');
  let told = null;
  s.onConflict = c => { told = c; };
  g.queue.push([412, { detail: { current: { id: 'em2', name: 'Theirs' }, etag: '"t9"' } }, {}]);
  await s.devWrite('em2', 'PATCH', '/api/devices/em2?dry_run=true', { sections: { identity: { name: 'x' } } });
  check('dry run: a 412 is told like any other (nothing saved, the tag kept)', told && told.current.name === 'Theirs' && s.etag === '"t1"');
}

// a write with a tag of its own (a device the page does not hold — the
// Measurements page assigning an uploaded map, L3-F11)
{
  const g = gateway();
  const s = M.createStore({ fetch: g.fetch });
  s.open('em2', { id: 'em2' }, '"t1"');
  g.queue.push([200, { status: 'updated', device: { id: 'inv1' } }, checked('"i2"')]);
  await s.devWrite('inv1', 'PATCH', '/api/devices/inv1', { sections: { model: { template: 'x' } } }, { ifMatch: '"i1"' });
  check('own tag: sent as If-Match for a device not held', g.last().headers['If-Match'] === '"i1"');
  check('own tag: nothing held moves (another device)', s.id === 'em2' && s.etag === '"t1"');
  g.queue.push([200, {}, checked('"t2"')]);
  await s.devWrite('em2', 'PATCH', '/api/devices/em2', { sections: {} }, { ifMatch: '"zzz"' });
  check('own tag: the held device always sends the held tag', g.last().headers['If-Match'] === '"t1"' && s.etag === '"t2"');
}

console.log(`\n${n - failed}/${n} passed`);
process.exit(failed ? 1 : 0);
