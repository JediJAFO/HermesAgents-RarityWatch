/* Token-free WhatsApp delivery for confirmed new completion-watch listings. */
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
function retryWindowsBusy(action) {
  for (let attempt = 0; ; attempt++) {
    try { return action(); }
    catch (error) {
      if (!['EBUSY','EPERM'].includes(error.code) || attempt >= 5) throw error;
      const until = Date.now() + 25 * (attempt + 1);
      while (Date.now() < until) {}
    }
  }
}
function releaseOwnedExecutionLock(lockPath, token) {
  const ownerPath = path.join(lockPath, 'owner.json');
  let owner;
  try { owner = JSON.parse(fs.readFileSync(ownerPath, 'utf8')); }
  catch { return; }
  if (owner.token !== token) return;
  retryWindowsBusy(() => fs.unlinkSync(ownerPath));
  retryWindowsBusy(() => fs.rmdirSync(lockPath));
}
// Same fail-closed lease protocol as the collector and Python orchestrators.
function acquireExecutionLock() {
  const lockPath = path.join(ROOT, '.exotic-execution-lock');
  try {
    const owner = JSON.parse(fs.readFileSync(path.join(lockPath, 'owner.json'), 'utf8'));
    if (process.env.MCFARLANE_EXOTIC_LOCK_TOKEN && process.env.MCFARLANE_EXOTIC_LOCK_TOKEN === owner.token) return () => {};
  } catch {}
  try { fs.mkdirSync(lockPath); }
  catch (error) { if (error.code === 'EEXIST') return null; throw error; }
  const token = require('crypto').randomBytes(24).toString('hex');
  try { fs.writeFileSync(path.join(lockPath, 'owner.json'), JSON.stringify({pid:process.pid,token})); }
  catch (error) { fs.rmdirSync(lockPath); throw error; }
  return () => releaseOwnedExecutionLock(lockPath, token);
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
if (!pending || !pending.fingerprint || !pending.events.length) {
  process.stdout.write('[SILENT]');
  return;
}
if (pending.fingerprint === state.last_whatsapp_delivered_change_fingerprint) {
  delete state.pending_whatsapp_change;
  atomicJson(STATE, state);
  process.stdout.write('[SILENT]');
  return;
}
if (pending.emitted_execution_id) {
  process.stdout.write('[SILENT]');
  return;
}
const lines = ['*McFarlane Completion Watch — confirmed new completion listing*', `Confirmed: ${pending.at}`, ''];
for (const event of deliverableEvents(state, pending)) {
  const c = state.collections.find(x => event.watch_key && x.watch_key === event.watch_key)
    || state.collections.find(x => x.name === event.name && (x.rarity || 'Exotic') === (event.rarity || 'Exotic'));
  if (!c) continue;
  const x = event.listing;
  lines.push(`• *[${c.rarity || 'Exotic'}] ${c.name}* — New listing | *🟢 YES* | *${Number(x.price).toLocaleString()} ${cur(x.currency)}* | Last: ${sale(c)}`);
}
if (lines.length <= 3) {
  delete state.pending_whatsapp_change;
  atomicJson(STATE, state);
  process.stdout.write('[SILENT]');
  return;
}
const text=lines.join('\r\n')+'\r\n';
const executionId = process.env.MCFARLANE_WHATSAPP_EXECUTION_ID;
if (!executionId) throw new Error('WhatsApp execution identity is unavailable; queue retained');
fs.writeFileSync(OUTPUT, text, 'utf8');
pending.emitted_execution_id = executionId;
pending.emitted_at = now();
atomicJson(STATE, state);
process.stdout.write(text);
}
function hasPendingChange() {
  try { return Boolean(JSON.parse(fs.readFileSync(STATE, 'utf8')).pending_whatsapp_change); }
  catch { return true; }
}
if (!hasPendingChange()) process.stdout.write('[SILENT]');
else {
  const release = acquireExecutionLock();
  if (!release) process.stdout.write('[SILENT]');
  else { try { deliver(); } finally { release(); } }
}
