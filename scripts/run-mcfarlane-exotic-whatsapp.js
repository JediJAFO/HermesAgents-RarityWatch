/* Token-free WhatsApp changes-only delivery stage for McFarlane Exotic Watch. */
const fs = require('fs');
const path = require('path');
const ROOT = '__HERMES_HOME__/price-watches';
const {deliverableEvents} = require(path.join(ROOT, 'exotic_whatsapp_policy.js'));
const STATE = path.join(ROOT, 'mcfarlane-exotics.json');
const OUTPUT = path.join(ROOT, 'mcfarlane-whatsapp-status.md');
function atomicJson(file, value) { const tmp = `${file}.${process.pid}.tmp`; fs.writeFileSync(tmp, JSON.stringify(value, null, 2) + '\n', 'utf8'); fs.renameSync(tmp, file); }
function now() { return new Date().toISOString(); }
function cur(x) { return x === 'POLYGON' ? 'POL' : x; }
function sale(c) { const s=c.last_exotic_sale; return s ? `${Number(s.price).toLocaleString()} ${cur(s.currency)}, ${s.activity_time}` : 'Not yet verified'; }
// Same fail-closed lease protocol as the collector and Python orchestrators.
function acquireExecutionLock() {
  const lockPath = path.join(ROOT, '.exotic-execution-lock');
  try {
    const owner = JSON.parse(fs.readFileSync(path.join(lockPath, 'owner.json'), 'utf8'));
    if (process.env.MCFARLANE_EXOTIC_LOCK_TOKEN && process.env.MCFARLANE_EXOTIC_LOCK_TOKEN === owner.token) return () => {};
  } catch {}
  try { fs.mkdirSync(lockPath); }
  catch (error) { if (error.code === 'EEXIST') return null; throw error; }
  try { fs.writeFileSync(path.join(lockPath, 'owner.json'), JSON.stringify({pid:process.pid,token:require('crypto').randomBytes(24).toString('hex')})); }
  catch (error) { fs.rmdirSync(lockPath); throw error; }
  return () => { fs.unlinkSync(path.join(lockPath, 'owner.json')); fs.rmdirSync(lockPath); };
}
function deliver() {
const state = JSON.parse(fs.readFileSync(STATE, 'utf8'));
const pending = state.pending_whatsapp_change;
// Legacy collection fingerprints have no before/after evidence. Drop only the
// unclassifiable queue under the lease, preserving baselines and delivered dedupe.
if (pending && (pending.schema_version !== 2 || !Array.isArray(pending.events))) {
  delete state.pending_whatsapp_change;
  atomicJson(STATE, state);
  process.stdout.write('[SILENT]');
  return;
}
if (!pending || !pending.fingerprint || pending.fingerprint === state.last_whatsapp_delivered_change_fingerprint || !pending.events.length) {
  process.stdout.write('[SILENT]');
  return;
}
const lines = ['*McFarlane Exotic Watch — verified changes*', `Verified: ${pending.at}`, ''];
for (const event of deliverableEvents(state, pending)) {
  const c = state.collections.find(x => x.name === event.name); if (!c) continue;
  const x = event.listing;
  const label = event.kind === 'new_listing' ? 'New listing' : 'Lower price';
  lines.push(`• *${c.name}* — ${label} | *🟢 YES* | *🟢 ${Number(x.price).toLocaleString()} ${cur(x.currency)}* | Last: ${sale(c)}`);
}
if (lines.length <= 3) {
  delete state.pending_whatsapp_change;
  atomicJson(STATE, state);
  process.stdout.write('[SILENT]');
  return;
}
const text=lines.join('\r\n')+'\r\n';
fs.writeFileSync(OUTPUT, text, 'utf8');
state.last_whatsapp_delivered_change_fingerprint = pending.fingerprint;
state.last_whatsapp_delivery_at = now();
delete state.pending_whatsapp_change;
atomicJson(STATE, state);
process.stdout.write(text);
}
const release = acquireExecutionLock();
if (!release) process.stdout.write('[SILENT]');
else { try { deliver(); } finally { release(); } }
