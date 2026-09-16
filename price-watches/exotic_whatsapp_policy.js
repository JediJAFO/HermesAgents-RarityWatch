/* Destination-specific event policy. Call only after independent promotion. */
function validPrice(value) {
  return (typeof value === 'number' || (typeof value === 'string' && value.trim() !== '')) && Number.isFinite(Number(value)) && Number(value) >= 0;
}
function lowerPrice(previous, listing) {
  return previous && String(previous.token_id) === String(listing.token_id) &&
    typeof listing.currency === 'string' && listing.currency.length > 0 && previous.currency === listing.currency &&
    validPrice(previous.price) && validPrice(listing.price) && Number(listing.price) < Number(previous.price);
}
function confirmedEvents(name, prior, current, at) {
  const old = new Map((prior.listings || []).map(x => [String(x.token_id), x]));
  return (current.listings || []).flatMap(listing => {
    const previous = old.get(String(listing.token_id));
    const kind = !previous ? 'new_listing' : lowerPrice(previous, listing) ? 'lower_price' : null;
    return kind ? [{name, at, kind, listing, previous: previous || null}] : [];
  });
}
function enqueue(state, events, at) {
  if (!events.length) return;
  const prior = state.pending_whatsapp_change;
  const queued = prior?.schema_version === 2 && Array.isArray(prior.events) && prior.fingerprint !== state.last_whatsapp_delivered_change_fingerprint ? prior.events : [];
  const combined = [...queued, ...events];
  const fingerprint = JSON.stringify(combined);
  if (fingerprint !== state.last_whatsapp_delivered_change_fingerprint)
    state.pending_whatsapp_change = {schema_version: 2, at, events: combined, fingerprint};
}
function deliverableEvents(state, pending) {
  if (pending?.schema_version !== 2 || !Array.isArray(pending.events)) return [];
  const eligible = pending.events.filter(event => {
    const listing = event?.listing;
    if (!listing || listing.token_id == null || String(listing.token_id) === '' ||
        !validPrice(listing.price) || typeof listing.currency !== 'string' || !listing.currency) return false;
    if (!(event.kind === 'new_listing' && event.previous === null) &&
        !(event.kind === 'lower_price' && lowerPrice(event.previous, listing))) return false;
    const collection = state.collections.find(c => c.name === event.name);
    // A delayed alert must still describe the current verified offer, not a
    // subsequently removed/increased/repriced listing or a removed collection.
    return collection?.baseline?.for_sale && (collection.baseline.listings || []).some(x =>
      String(x.token_id) === String(listing.token_id) && x.currency === listing.currency &&
      validPrice(x.price) && Number(x.price) === Number(listing.price));
  });
  // Multiple confirmations before handoff still produce one current offer row.
  return [...new Map(eligible.map(event => [JSON.stringify([event.name, String(event.listing.token_id)]), event])).values()];
}
module.exports = {confirmedEvents, enqueue, lowerPrice, deliverableEvents};
