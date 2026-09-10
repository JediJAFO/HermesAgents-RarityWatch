/* Deterministic McFarlane Exotic collector: isolated Chrome only. */
const fs = require('fs');
const path = require('path');
const { chromium } = require('C:/Users/jltfo/AppData/Local/hermes/hermes-agent/node_modules/playwright');

const ROOT = 'C:/Users/jltfo/AppData/Local/hermes/price-watches';
const STATE = path.join(ROOT, 'mcfarlane-exotics.json');
const RUN = path.join(ROOT, 'mcfarlane-deterministic-run.json');
const ALIASES = path.join(ROOT, 'mcfarlane-wallet-aliases.json');
const CDP = 'http://[::1]:9222';
const RARIBLE_API = 'https://api.rarible.org/v0.1/orders/sell/byItem';
const GAP_MS = 10000; // User-required minimum pacing between marketplace navigations.
const LOAD_SETTLE_MS = 4000;
// Two measured attempts are sufficient for a transient UI shell. Longer retry
// loops made a 10-collection run exceed its useful execution window.
const MAX_ATTEMPTS = 2;
const BATCH_DEADLINE_MS = 330000; // retain state rather than running indefinitely.
const ET = new Intl.DateTimeFormat('sv-SE', { timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const now = () => {
  const p = Object.fromEntries(ET.formatToParts(new Date()).filter(x => x.type !== 'literal').map(x => [x.type, x.value]));
  return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}:${p.second}-04:00`;
};
function atomicJson(file, value) {
  const tmp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(value, null, 2) + '\n', 'utf8');
  fs.renameSync(tmp, file);
}
function atomicText(file, value) {
  const tmp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, value, 'utf8');
  fs.renameSync(tmp, file);
}
function canonical(x) { return JSON.stringify(x); }
function sameFilteredUrl(actual, expected) {
  try {
    const a = new URL(actual), e = new URL(expected);
    if (a.origin !== e.origin || a.pathname !== e.pathname) return false;
    const normalize = u => [...u.searchParams.entries()].sort((x, y) => x.join('=').localeCompare(y.join('=')));
    return canonical(normalize(a)) === canonical(normalize(e));
  } catch { return false; }
}
function explicitBlock(text) {
  const t = text.toLowerCase();
  return /\b429\b|captcha|challenge|access denied|you have been blocked|temporarily blocked/.test(t);
}
function priceFrom(text) {
  const m = text.replace(/\u00a0/g, ' ').match(/([\d,]+(?:\.\d+)?)\s*(POLYGON|MATIC|POL)\b/i);
  if (!m) return null;
  const price = Number(m[1].replace(/,/g, ''));
  return Number.isFinite(price) ? { price, currency: 'POLYGON' } : null;
}
function productTextBeforeRecommendations(text) {
  // Recommended cards belong to other tokens, including their traits/offers.
  return String(text || '').split(/\b(?:more from (?:this |the )?collection|you (?:may|might) also like|recommendations|recommended (?:items|for you)|similar items)\b/i)[0];
}
function buyNowPriceFromProduct(text) {
  text = productTextBeforeRecommendations(text);
  if (/\bOPEN FOR BIDS\b/i.test(text)) return null;
  // Product pages can contain historical prices too; use only the price tied to
  // the live BUY NOW control, never the first generic POL amount on the page.
  const match = String(text || '').match(/BUY\s+NOW(?:\s+FOR)?\s*(?:\n|\s)+([\d,]+(?:\.\d+)?)\s*(POLYGON|MATIC|POL)\b/i);
  if (!match) return null;
  const price = Number(match[1].replace(/,/g, ''));
  return Number.isFinite(price) ? { price, currency: 'POLYGON' } : null;
}
function normalizeListings(raw) {
  const out = [];
  const seen = new Set();
  for (const row of raw) {
    const href = row.href || '';
    const idMatch = href.match(/:([0-9]+)\/?$/);
    const offer = priceFrom(row.text || '');
    const text = (row.text || '').replace(/\s+/g, ' ').trim();
    const fixedPriceNoBids = Boolean(offer) && /Highest bid\s*(?:\n|\s)+No bids yet/i.test(row.text || '');
    if (!idMatch || !offer || (!/BUY NOW/i.test(text) && !fixedPriceNoBids) || /OPEN FOR BIDS/i.test(text)) continue;
    if (seen.has(href)) continue;
    seen.add(href);
    const name = text.replace(/BUY NOW/ig, '').replace(/[\d,]+(?:\.\d+)?\s*(POLYGON|MATIC|POL)\b/ig, '').trim().slice(0, 300) || 'Internal listing name';
    out.push({ token_id: idMatch[1], name: `BUY NOW ${name}`, price: offer.price, currency: offer.currency, url: href });
  }
  out.sort((a, b) => a.token_id.localeCompare(b.token_id, undefined, { numeric: true }) || a.url.localeCompare(b.url));
  return out;
}
function ownerFromProductText(text) {
  // The token page's current owner is a seller cross-check for an active listing.
  const full = text.match(/(?:owned\s+by|owner)\s*[:\-]?\s*(?:\n|\s)*(0x[a-f0-9]{40})\b/i);
  if (full) return { wallet: full[1] };
  const masked = text.match(/(?:owned\s+by|owner)[\s\S]{0,80}?(?:\.\.\.|…)([a-f0-9]{4,9})\b/i);
  return masked ? { display: `...${masked[1].toLowerCase()}` } : null;
}
async function extractObservation(page, collection) {
  const actualUrl = page.url();
  const body = await page.locator('body').innerText({ timeout: 10000 }).catch(() => '');
  if (explicitBlock(body)) return { kind: 'GLOBAL_BLOCK', reason: 'explicit marketplace block evidence' };
  if (!sameFilteredUrl(actualUrl, collection.source_url)) return { kind: 'UNAVAILABLE', reason: 'unexpected filtered-URL redirect', actual_url: actualUrl };

  // The current storefront omits the visible filter-control widgets even when
  // the exact canonical filtered URL is retained.  The stronger operational
  // proof is the exact URL plus BUY NOW card extraction and per-item trait
  // verification below; do not treat absent cosmetic controls as a change.

  // Wait briefly for either a listing card or an explicit empty-state signal.
  // A loading shell must never be promoted as a verified zero-listing result.
  await page.waitForFunction(() => {
    const text = document.body ? document.body.innerText : '';
    return Boolean(document.querySelector('a[href*="/token/"]')) || /\b(no items found|no results found|nothing found|0 items)\b/i.test(text);
  }, { timeout: 10000 }).catch(() => {});
  const tokenAnchors = await page.locator('a[href*="/token/"]').evaluateAll(nodes => nodes.map(a => {
    let p = a; let text = '';
    for (let i = 0; i < 6 && p; i++, p = p.parentElement) {
      const candidate = (p.innerText || '').trim();
      if (candidate.length > text.length) text = candidate;
      if (/BUY NOW/i.test(candidate) && candidate.length < 1400) { text = candidate; break; }
    }
    return { href: a.href, text };
  })).catch(() => []);
  const listings = normalizeListings(tokenAnchors);
  const explicitEmpty = /\b(no items found|no results found|nothing found|0 items)\b/i.test(body);
  // If token cards or BUY NOW text rendered but card parsing yielded nothing,
  // preserve the last known-good baseline rather than treating it as a sale.
  if (!listings.length && (tokenAnchors.length || /\bBUY NOW\b/i.test(body))) {
    return { kind: 'UNAVAILABLE', reason: 'listing-card extraction inconsistent with rendered BUY NOW content', actual_url: actualUrl };
  }
  // A silent/unfinished filtered page is not evidence of zero available items.
  if (!listings.length && !explicitEmpty) {
    return { kind: 'UNAVAILABLE', reason: 'no explicit empty-listing evidence after render wait', actual_url: actualUrl };
  }
  return { kind: 'VERIFIED', baseline: { for_sale: listings.length > 0, listing_count: listings.length, listings }, actual_url: actualUrl };
}
async function verifyExoticTraits(page, listings, deadlineAt) {
  const active = [];
  for (const listing of listings) {
    if (Date.now() + GAP_MS + 30000 > deadlineAt) return { ok: false, reason: 'batch time budget reached before product rarity verification' };
    // A product page is the definitive rarity check. This is also a marketplace
    // navigation, so it receives the same measured cooldown as collection URLs.
    await sleep(GAP_MS);
    try {
      await page.goto(listing.url, { waitUntil: 'domcontentloaded', timeout: 25000 });
      await sleep(LOAD_SETTLE_MS);
      const text = await page.locator('body').innerText({ timeout: 10000 }).catch(() => '');
      if (explicitBlock(text)) return { ok: false, global: true, reason: 'explicit marketplace block evidence on product page' };
      if (!sameFilteredUrl(page.url(), listing.url)) return { ok: false, reason: `unexpected product URL for token ${listing.token_id}` };
      const productText = productTextBeforeRecommendations(text);
      if (/\bOPEN FOR BIDS\b/i.test(productText)) continue;
      if (!/\bRarity\s*(?:\n|\s)+Exotic\b/i.test(productText)) return { ok: false, reason: `product trait did not verify Exotic for token ${listing.token_id}` };
      const livePrice = buyNowPriceFromProduct(productText);
      if (!livePrice) return { ok: false, reason: `product page did not expose a live BUY NOW price for token ${listing.token_id}` };
      listing.price = livePrice.price;
      listing.currency = livePrice.currency;
      const owner = ownerFromProductText(productText);
      if (owner?.wallet) Object.assign(listing, resolveSeller(owner.wallet), { seller_lookup: 'token-page current owner' });
      else if (owner?.display) { listing.seller_display = owner.display; listing.seller_lookup = 'token-page masked current owner'; }
      active.push(listing);
    } catch (error) {
      return { ok: false, reason: `product rarity verification error: ${String(error.message || error).slice(0, 180)}` };
    }
  }
  listings.splice(0, listings.length, ...active);
  await enrichListingSellers(listings, deadlineAt);
  return { ok: true };
}

async function enrichListingSellers(listings, deadlineAt) {
  // Active sell orders are the authoritative seller source. A lookup failure
  // never invalidates a browser-verified listing, because seller display is
  // enrichment rather than listing/rarity evidence.
  const key = process.env.RARIBLE_API_KEY;
  if (!key) return;
  for (const listing of listings) {
    if (Date.now() + GAP_MS + 15000 > deadlineAt) return;
    await sleep(GAP_MS);
    try {
      const itemId = listing.url.split('/token/')[1] || '';
      if (!/^POLYGON:0x[0-9a-f]+:[0-9]+$/i.test(itemId)) { listing.seller_lookup = 'invalid item identity'; continue; }
      const query = new URLSearchParams({ itemId, status: 'ACTIVE', size: '20' });
      const response = await fetch(`${RARIBLE_API}?${query}`, { headers: { 'X-API-KEY': key, 'Accept': 'application/json', 'User-Agent': 'Mozilla/5.0' } });
      if (!response.ok) { listing.seller_lookup = `order API HTTP ${response.status}`; continue; }
      const data = await response.json();
      const orders = Array.isArray(data.orders) ? data.orders : [];
      const matching = orders.find(order => order && order.status === 'ACTIVE' && typeof order.maker === 'string');
      if (!matching) { listing.seller_lookup = 'no active seller order returned'; continue; }
      Object.assign(listing, resolveSeller(matching.maker), { seller_lookup: 'Rarible active sell order maker' });
    } catch (error) {
      listing.seller_lookup = 'seller lookup unavailable';
    }
  }
}

async function observeWithRetries(page, collection, waitBeforeNav, deadlineAt, recoverPage) {
  let last = null;
  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
    const needsGap = attempt > 1 || waitBeforeNav();
    if (Date.now() + (needsGap ? GAP_MS : 0) + 30000 > deadlineAt) return { kind: 'UNAVAILABLE', reason: 'batch time budget reached before collection check', attempts: attempt - 1 };
    if (needsGap) await sleep(GAP_MS);
    try {
      if (page.isClosed?.() && recoverPage) page = await recoverPage(deadlineAt);
      await page.goto(collection.source_url, { waitUntil: 'domcontentloaded', timeout: 25000 });
      await sleep(LOAD_SETTLE_MS);
      const result = await extractObservation(page, collection);
      result.attempts = attempt;
      if (result.kind === 'GLOBAL_BLOCK') return result;
      if (result.kind === 'VERIFIED') {
        const traits = await verifyExoticTraits(page, result.baseline.listings, deadlineAt);
        if (traits.global) return { kind: 'GLOBAL_BLOCK', reason: traits.reason, attempts: attempt };
        if (traits.ok) {
          result.baseline.listing_count = result.baseline.listings.length;
          result.baseline.for_sale = result.baseline.listings.length > 0;
          return result;
        }
        last = { kind: 'UNAVAILABLE', reason: traits.reason, attempts: attempt };
        continue;
      }
      last = result;
    } catch (error) {
      last = { kind: 'UNAVAILABLE', reason: `navigation/render error: ${String(error.message || error).slice(0, 180)}`, attempts: attempt };
    }
  }
  return last || { kind: 'UNAVAILABLE', reason: 'no observation produced', attempts: MAX_ATTEMPTS };
}
function stabilizeListingNames(prior, observed) {
  // Card wrappers change presentation text. Preserve known display metadata only
  // while the token and its active price remain unchanged.
  const known = new Map((prior.listings || []).map(x => [String(x.token_id), x]));
  return {
    for_sale: Boolean(observed.for_sale),
    listing_count: observed.listing_count,
    listings: (observed.listings || []).map(x => {
      const old = known.get(String(x.token_id));
      const samePrice = old && Number(old.price) === Number(x.price) && old.currency === x.currency;
      return {
        ...x,
        name: old?.name || x.name,
        ...(samePrice && old?.price_set_time ? { price_set_time: old.price_set_time, price_set_source: old.price_set_source } : {}),
        ...(!x.seller_wallet && !x.seller_name_tag && !x.seller_display && old?.seller_name_tag ? { seller_name_tag: old.seller_name_tag, seller_lookup: old.seller_lookup } : {}),
        ...(!x.seller_wallet && !x.seller_name_tag && !x.seller_display && old?.seller_display ? { seller_display: old.seller_display, seller_lookup: old.seller_lookup } : {}),
      };
    })
  };
}
function comparableBaseline(baseline) {
  return {
    for_sale: Boolean(baseline.for_sale),
    listing_count: baseline.listing_count,
    listings: (baseline.listings || []).map(x => ({ token_id: x.token_id, name: x.name, price: x.price, currency: x.currency, url: x.url }))
  };
}
function currency(v) { return v === 'POLYGON' ? 'POL' : v; }
function normalizedWallet(value) { return typeof value === 'string' && value.trim() ? value.split(':').pop().toLowerCase().trim() : null; }
function resolveSeller(wallet) {
  const normalized = normalizedWallet(wallet);
  if (!normalized) return { seller_wallet: null, seller_name_tag: null };
  let aliases;
  try { aliases = JSON.parse(fs.readFileSync(ALIASES, 'utf8')); } catch { return { seller_wallet: normalized, seller_name_tag: null }; }
  const exact = aliases.exact_aliases || {};
  if (typeof exact[normalized] === 'string') return { seller_wallet: normalized, seller_name_tag: exact[normalized] };
  const matches = (aliases.masked_aliases || []).filter(x => x && typeof x.prefix === 'string' && typeof x.suffix === 'string' && normalized.startsWith(x.prefix.toLowerCase()) && normalized.endsWith(x.suffix.toLowerCase())).map(x => x.name_tag).filter(x => typeof x === 'string');
  return { seller_wallet: normalized, seller_name_tag: matches.length === 1 ? matches[0] : null };
}
function sellerText(listing) { return listing.seller_name_tag || (listing.seller_wallet ? `...${listing.seller_wallet.slice(-9)}` : 'Seller not recorded'); }
function buyerText(s) {
  const tagged = s.buyer_display || s.buyer_name_tag;
  if (tagged) return tagged;
  const wallet = normalizedWallet(s.buyer_wallet || s.buyer);
  return wallet ? `...${wallet.slice(-9)}` : 'Buyer not recorded';
}
function saleTimeText(s) {
  const raw = s?.activity_date;
  if (typeof raw !== 'string' || !raw.trim()) return s?.activity_time || 'time not recorded';
  const instant = new Date(raw);
  if (Number.isNaN(instant.getTime())) return s?.activity_time || 'time not recorded';
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23', timeZoneName: 'short'
  }).formatToParts(instant).reduce((out, x) => ({ ...out, [x.type]: x.value }), {});
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute} ${parts.timeZoneName}`;
}
function saleText(c) {
  const s = c.last_exotic_sale;
  if (!s) return 'Not yet verified';
  const usd = Number(s.usd_amount);
  const usdText = Number.isFinite(usd) ? ` ($${usd.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })})` : '';
  return `${Number(s.price).toLocaleString()} ${currency(s.currency)}${usdText}, ${saleTimeText(s)} by ${buyerText(s)}`;
}
function listingDetails(c) { return (c.baseline.listings || []).map(x => `${Number(x.price).toLocaleString()} ${currency(x.currency)} by ${sellerText(x)} (set ${x.price_set_time || 'time not recorded'})`).join(', ') || '—'; }
function report(state, outcome) {
  const active = state.collections.filter(c => c.baseline && c.baseline.for_sale);
  const lines = [];
  if (outcome.failed_collections.length) {
    lines.push(`Partial check — ${outcome.failed_collections.map(x => `**${x.name}** (${x.type})`).join('; ')}. Last-known-good rows were retained.`);
    lines.push('');
  }
  lines.push(outcome.changed ? 'Changes detected —' : '**McFarlane Exotic Watch — no changes**');
  lines.push(`Last complete check: ${state.last_successful_monitor_run || '—'}`);
  lines.push('');
  const rows = outcome.changed ? state.collections : active;
  for (const c of rows) {
    const b = c.baseline || { for_sale: false, listing_count: 0, listings: [] };
    if (b.for_sale) lines.push(`• **${c.name}** — **🟢 YES**; ${b.listing_count} listing(s); **${listingDetails(c)}**; last: ${saleText(c)}; [View](<${c.source_url}>)`);
    else if (outcome.changed) lines.push(`• **${c.name}** — No; 0 listing(s); —; last: ${saleText(c)}`);
  }
  if (!rows.length) lines.push('No active Exotic buy-now listings.');
  return lines.join('\r\n') + '\r\n';
}

// A primary scheduled full run always reports its first result. Targeted
// recovery passes are silent while incomplete and emit once when they clear
// the outstanding checks from that primary run.
function recordPartialDeliveryPolicy(state, outcome) {
  const prior = state.partial_discord_notice;
  if (outcome.scope === 'full') {
    if (!outcome.complete) {
      state.partial_discord_notice = {
        started_at: outcome.at,
        scope: outcome.scope,
        failures: (outcome.failed_collections || []).map(x => x.name),
      };
    } else if (prior) {
      delete state.partial_discord_notice;
    }
    return true;
  }
  // Targeted recovery/confirmation: repeated incomplete attempts are internal.
  if (!outcome.complete) return false;
  if (prior) delete state.partial_discord_notice;
  return true;
}
function acquireExecutionLock(lockPath = path.join(ROOT, '.exotic-execution-lock')) {
  const token = process.env.MCFARLANE_EXOTIC_LOCK_TOKEN;
  try {
    const owner = JSON.parse(fs.readFileSync(path.join(lockPath, 'owner.json'), 'utf8'));
    if (token && token === owner.token) return () => {};
  } catch {}
  try { fs.mkdirSync(lockPath); }
  catch (error) { if (error.code === 'EEXIST') return null; throw error; }
  try {
    fs.writeFileSync(path.join(lockPath, 'owner.json'), JSON.stringify({pid: process.pid, token: require('crypto').randomBytes(24).toString('hex')}));
  } catch (error) { fs.rmdirSync(lockPath); throw error; }
  // Do not time-expire another process's lease. Hard kills deliberately fail closed.
  return () => { fs.unlinkSync(path.join(lockPath, 'owner.json')); fs.rmdirSync(lockPath); };
}
async function main() {
  const release = acquireExecutionLock();
  if (!release) { process.exitCode = 75; return; }
  try { await collectMain(); } finally { release(); }
}
async function collectMain() {
  const state = JSON.parse(fs.readFileSync(STATE, 'utf8'));
  const run = { at: now(), collector: 'deterministic-playwright-cdp-v1', results: [], global_stop: false };
  let browser, page, lastNavigation = false;
  const requestedNames = new Set((process.env.MCFARLANE_COLLECTION_NAMES || '').split('|').map(x => x.trim()).filter(Boolean));
  const limit = Math.min(state.collections.length, Number(process.env.MCFARLANE_LIMIT || state.collections.length));
  const selectedCollections = requestedNames.size
    ? state.collections.filter(c => requestedNames.has(c.name))
    : state.collections.slice(0, limit);
  run.scope = !requestedNames.size && !process.env.MCFARLANE_RUN_KIND && selectedCollections.length === state.collections.length ? 'full' : 'targeted';
  if (run.scope === 'full') state.last_primary_exotic_run_id = require('crypto').randomUUID();
  const selectedNames = new Set(selectedCollections.map(c => c.name));
  const outstanding = run.scope === 'targeted'
    ? (state.last_monitor_outcome?.failed_collections || []).filter(x => !selectedNames.has(x.name)) : [];
  try {
    browser = await chromium.connectOverCDP(CDP, { timeout: 20000 });
    const context = browser.contexts()[0];
    // Own only the newly created page. Other tabs may belong to another task.
    page = await context.newPage();
    page.setDefaultTimeout(10000);
    const startedAt = Date.now();
    const deadlineAt = startedAt + BATCH_DEADLINE_MS;
    let reconnects = 0;
    const recoverPage = async deadline => {
      if (reconnects >= 1 || Date.now() + 50000 > deadline) throw new Error('owned-page reconnect budget exhausted');
      reconnects++;
      if (!browser.isConnected()) browser = await chromium.connectOverCDP(CDP, { timeout: 20000 });
      page = await browser.contexts()[0].newPage();
      page.setDefaultTimeout(10000);
      return page;
    };

    for (const collection of selectedCollections) {
      if (Date.now() - startedAt > BATCH_DEADLINE_MS) {
        run.timed_out = true;
        run.results.push({ name: collection.name, kind: 'UNAVAILABLE', reason: 'batch time budget reached before collection check' });
        continue;
      }
      const result = await observeWithRetries(page, collection, () => lastNavigation, deadlineAt, recoverPage);
      lastNavigation = true;
      run.results.push({ name: collection.name, ...result });
      // Persist progress so an interrupted diagnostic has useful evidence.
      atomicJson(RUN, run);
      if (result.kind === 'GLOBAL_BLOCK') { run.global_stop = true; run.global_reason = result.reason; break; }
    }
  } catch (error) {
    run.fatal_error = String(error.message || error).slice(0, 300);
  } finally {
    if (page) await page.close().catch(() => {});
    if (browser) await browser.close().catch(() => {});
  }
  atomicJson(RUN, run);
  if (run.global_stop || run.fatal_error || run.results.length !== selectedCollections.length) {
    const reason = run.global_reason || run.fatal_error || 'incomplete deterministic result set';
    const outcome = { at: run.at, complete: false, changed: false, scope: run.scope, failed_collections: [...outstanding, ...selectedCollections.map(c => ({ name: c.name, type: reason }))] };
    state.last_monitor_outcome = outcome;
    const deliver = recordPartialDeliveryPolicy(state, outcome);
    atomicJson(STATE, state);
    // Always save the diagnostic; only the initial partial incident goes to Discord.
    const text = report(state, outcome);
    atomicText(path.join(ROOT, 'mcfarlane-discord-deterministic-status.md'), text);
    if (deliver) process.stdout.write(text);
    return;
  }

  const failures = [...outstanding], changed = [];
  const candidates = state.pending_exotic_change_candidates || {};
  for (const collection of state.collections) {
    const result = run.results.find(x => x.name === collection.name);
    if (!result) continue;
    if (result.kind !== 'VERIFIED') { failures.push({ name: collection.name, type: result.reason || 'local failure' }); continue; }
    const prior = collection.baseline || { for_sale: false, listing_count: 0, listings: [] };
    const stableBaseline = stabilizeListingNames(prior, result.baseline);
    const candidateFingerprint = canonical(comparableBaseline(stableBaseline));
    if (canonical(comparableBaseline(prior)) !== candidateFingerprint) {
      // Promote a change only after the same normalized observation appears in
      // two independent interval runs. This avoids an extra slow browser pass
      // and makes a transient card/render defect incapable of alerting.
      if (candidates[collection.name] === candidateFingerprint) {
        changed.push(collection.name);
        collection.baseline = stableBaseline;
        collection.last_successful_observation = run.at;
        delete candidates[collection.name];
      } else {
        candidates[collection.name] = candidateFingerprint;
        failures.push({ name: collection.name, type: 'candidate listing change held for next-interval confirmation' });
      }
      continue;
    }
    delete candidates[collection.name];
    // Seller enrichment is display data, not an alert-worthy listing change.
    collection.baseline = stableBaseline;
    collection.last_successful_observation = run.at;
  }
  state.pending_exotic_change_candidates = candidates;
  const outcome = { at: run.at, complete: failures.length === 0, changed: changed.length > 0, scope: run.scope, failed_collections: failures, collector: run.collector };
  state.last_monitor_outcome = outcome;
  const deliver = recordPartialDeliveryPolicy(state, outcome);
  if (outcome.complete && run.scope === 'full') state.last_successful_monitor_run = run.at;
  if (changed.length) {
    const fingerprint = canonical(state.collections.map(c => [c.name, c.baseline]));
    if (state.last_whatsapp_delivered_change_fingerprint !== fingerprint) state.pending_whatsapp_change = { at: run.at, collections: changed, fingerprint };
    state.last_alert_fingerprint = fingerprint;
  }
  atomicJson(STATE, state);
  const text = report(state, outcome);
  atomicText(path.join(ROOT, 'mcfarlane-discord-deterministic-status.md'), text);
  if (deliver) process.stdout.write(text);
}
main().catch(error => { console.error(`Deterministic monitor fatal error: ${error.stack || error}`); process.exitCode = 1; });
