# Product Requirements Document — McFarlane Exotic Watch

**Version:** 1.0
**Last updated:** 2026-08-26 10:50 EDT
**Status:** Active / implemented baseline
**System of record:** `C:/Users/jltfo/AppData/Local/hermes/price-watches/mcfarlane-exotics.json`

## 1. Purpose

Monitor McFarlane Toys Digital marketplace collections for active **buy-now** listings with **Rarity = Exotic**, retain a verified historical last-Exotic-sale reference, and deliver clear, low-noise updates through Discord and WhatsApp.

The product must let the user immediately identify collections that have an active Exotic listing while preserving detailed NFT identity data privately for reliable verification and comparison.

## 2. Goals

1. Check the configured collections at 12:00 AM, 8:00 AM, 12:00 PM, 4:00 PM, and 8:00 PM Eastern Time.
2. Track only active buy-now Exotic listings; never treat open bids as availability.
3. Preserve last-known-good data when a page fails, renders partially, redirects, or is blocked.
4. Make active listings visually prominent in both Discord and WhatsApp.
5. Deliver Discord status on every scheduled check; deliver WhatsApp only when verified data changed.
6. Avoid excessive marketplace traffic and reduce bot/rate-limit risk through strictly serial pacing.
7. Keep internal token/item detail available for validation while excluding it from user-facing status output.

## 3. Non-goals and boundaries

- Do not buy, bid, connect a wallet, authenticate to payment UI, or otherwise transact.
- Do not track bid-only listings.
- Do not infer a rarity from a collection floor price.
- Do not report an unverified listing, sale, or failed result as factual.
- Do not expose marketplace token IDs, item names, token URLs, contract IDs, or raw source URLs in routine user-facing status messages.
- Never send a WhatsApp message for a no-change check or failure-only check.

## 4. Current scope

The watch currently contains 10 collections. Collection identifiers and source URLs are stored only in the JSON state file. The public display names are:

1. Batman Beyond
2. Lobo's Spacehog
3. Superman Action Comics #1
4. Doomsday
5. Batman by Todd McFarlane
6. Martian Manhunter
7. Knightfall
8. Batman Year 2: Designed by Todd McFarlane
9. Bane: Knight Fall
10. Jim Lee's Superman

## 5. Source and filter requirements

### 5.1 Listing source

- Marketplace: McFarlane Toys Digital.
- Each collection has an exact, stored filtered source URL.
- The URL must request `inStockOnly=true` and `traits[Rarity][0]=Exotic` (encoded form is acceptable).
- The marketplace is client-rendered; listing pages must be inspected in a real browser, not with a static HTML extractor.

### 5.2 Successful listing-filter validation

A collection listing result is complete only when all conditions hold:

1. The rendered browser URL retains the stored buy-now and Exotic query parameters.
2. The listed-only/buy-now control is visibly active.
3. The Rarity control shows one active selection. The marketplace commonly renders this collapsed state as `Rarity 1`; combined with the exact stored Exotic URL, this is valid Exotic-filter evidence.
4. Rendered cards are available to inspect, or the confirmed filtered view contains no cards (a valid zero-listing result).

### 5.3 Listing normalization

For each complete collection result, store a deterministic sorted listing list containing:

- token ID (internal);
- item name (internal);
- price;
- currency (`POLYGON`);
- token URL (internal);
- `for_sale` boolean;
- `listing_count`.

A tracked change is any change to availability, count, token identity, token name, price, currency, or verified last-Exotic-sale data.

## 6. Marketplace request sequencing and anti-bot protection

### 6.1 Mandatory serial flow

1. Start with one collection source URL in one real browser tab/session.
2. Wait for the page to render and validate the filters.
3. Capture and normalize the listing result, or classify that collection attempt as a local failure.
4. Wait **at least 15 seconds** after every marketplace navigation/request.
5. Only after that cooldown, navigate to the next collection or retry the same collection.
6. Never batch collection URLs, open parallel marketplace tabs, or rely on tool latency as the cooldown.

The 15-second interval exceeds the required 10-second minimum.

### 6.2 Global protection response

If the marketplace presents a site-wide 429, CAPTCHA/challenge, or explicit access-denied/block page:

1. Stop all remaining marketplace requests immediately.
2. Do not retry rapidly.
3. Retain all prior verified state.
4. Mark the run as a global partial/block condition in Discord.
5. Do not create a WhatsApp change event.

An unexpected redirect—including a redirect to the generic `rarible.org/` API/marketing page—is **not** sufficient evidence of a bot block. Treat it as a local collection failure, retry that collection within the serial limit, retain its prior row if needed, and continue with remaining collections.

## 7. Per-collection fault isolation

A loading shell, timeout, extraction/navigation error, partial render, unexpected redirect, filter ambiguity, or collection-specific 5xx is a **local** failure.

For each local failure:

1. Retry only that collection, up to three total attempts.
2. Apply the 15-second serial cooldown after each attempt.
3. On final failure, save only an internal failure timestamp/diagnostic.
4. Preserve that collection's prior baseline, last successful observation timestamp, and last verified Exotic sale.
5. Continue with all remaining collections.

A failed collection must never stop successful observations for other collections and must never be replaced with zero listings or blank data.

## 8. Last Exotic sale logic

### 8.1 Authority and verification

1. Use the collection-level **Activity** view as the authoritative source for sale price and displayed activity time.
2. Inspect Sale rows newest-first.
3. A yellow lower-left thumbnail marker, or an asset path containing `/exotic/`, is definitive positive evidence that a candidate is Exotic and should be prioritized.
4. Absence of that marker is not negative evidence; unmarked candidates remain eligible for verification.
5. Open the candidate product page only to verify its `Rarity = Exotic` trait.
6. On confirmation, record the Activity row's displayed price, currency, and displayed time exactly as shown.
7. Stop reviewing older Activity rows after the first newest verified Exotic sale.

### 8.2 Incremental Activity review

1. Persist an internal fingerprint for the newest Activity Sale of any rarity: token ID, displayed price, and displayed time.
2. On a complete later Activity check, compare the newest fingerprint with the stored fingerprint.
3. If unchanged, retain the existing verified last-Exotic sale and do not inspect older Activity rows or token pages.
4. If changed or absent, perform the newest-first review above.
5. The any-rarity fingerprint is internal and never appears in tables or notifications.

### 8.3 Product History restriction

Product-page History can optionally double-check the already identified token. It must never establish the collection-level last Exotic sale; the collection Activity view remains authoritative.

## 9. Data persistence and rendering

### 9.1 Machine-readable state

The JSON state stores collection membership, source URLs, last-known-good normalized listings, per-row successful timestamps, internal token/item evidence, last Exotic sale evidence, latest Activity fingerprints, failure policy, delivery policy, and WhatsApp change-event deduplication.

### 9.2 User-facing display policy

User-facing status must show only:

- collection name;
- whether an Exotic listing is currently for sale;
- listing count;
- price(s) in `POL` (the internal/state currency remains `POLYGON`);
- last successful update time;
- `last` price/time;
- Discord View link only when a collection is currently for sale.

User-facing status must not show NFT item names, token IDs, token URLs, contract IDs, or raw source URLs.

### 9.3 Files

- State: `C:/Users/jltfo/AppData/Local/hermes/price-watches/mcfarlane-exotics.json`
- Local review table: `C:/Users/jltfo/nft_exotic_watch.md`
- Discord rendering helper/output: `render_mcfarlane_discord_status.py` / `mcfarlane-discord-status.md`
- WhatsApp rendering helper/output: `render_mcfarlane_whatsapp_status.py` / `mcfarlane-whatsapp-status.md`
- This PRD: `C:/Users/jltfo/Documents/McFarlane_Exotic_Watch_PRD.md`

All rendered status rows use CRLF physical line endings.

## 10. Notification requirements

### 10.1 Discord

- Send an update after every scheduled run: changed, no-change, partial, or global-block status.
- Use a compact native bullet list with one collection per line.
- **No-change output:** deliver the active-only status list. It includes only collections with a currently active Exotic buy-now listing and omits all inactive collections. If none are active, say `No active Exotic buy-now listings.`
- **Changed output:** deliver the full status list for every monitored collection, including inactive rows, so the user can review the complete change context.
- **Partial output:** begin `Partial check —`, name failures and confirm their prior values were retained. Use active-only output if there are no verified changes; use full output if a verified change exists.
- Active collections must display **🟢 YES** and **🟢 price** in bold. Discord ordinary messages do not support arbitrary text color; the green marker is the visual color cue.
- For active collections, use a masked no-embed link exactly like `[View](<https://...>)`. This is clickable, hides the full URL, and suppresses Discord's large marketplace preview card.
- Changed response begins: `Changes detected —`.

### 10.2 WhatsApp

- Send only for verified changes, using a downstream changes-only job and a deterministic pending-change fingerprint.
- For no pending change, output exactly `[SILENT]` so no WhatsApp message is delivered.
- Never send URLs or View links; WhatsApp renders URLs in full and degrades readability.
- Active collections must display `*🟢 YES*` and `*🟢 <price> POLYGON*`.
- WhatsApp supports bold formatting but not arbitrary text colors; the green marker provides the visual highlight.
- No-change and failure-only runs create no WhatsApp notification.

## 11. Scheduled jobs

| Job | Schedule | Destination | Purpose |
|---|---|---|---|
| McFarlane Exotic NFT Watch | 00:00, 08:00, 12:00, 16:00, 20:00 EDT | Discord | Main browser-based monitor and every-run report |
| McFarlane Exotic Watch — WhatsApp changes only | 15 minutes after each main schedule | WhatsApp | Deduplicated delivery of verified changes only |

## 12. End-to-end run sequence

1. Scheduler starts the Discord monitor at a configured Eastern-time interval.
2. Monitor loads the JSON state and its exact stored collection URLs.
3. Monitor visits collection pages one at a time in one browser tab.
4. Monitor validates filters, parses cards, and waits 15 seconds before each next marketplace request.
5. Each complete collection result updates only that collection's candidate state.
6. Each failed collection preserves prior values and allows the run to continue.
7. Monitor compares complete observations against prior baselines.
8. Monitor updates verified baselines and per-row timestamps only for successful collections.
9. Monitor reviews Activity only when needed by the newest-sale fingerprint rule.
10. Monitor writes internal state and regenerates local, Discord, and WhatsApp renderings.
11. Monitor selects the Discord rendering based on result: on a verified change, send the complete all-collection list; on a no-change run, send only currently active Exotic buy-now rows; on a partial run, use active-only unless a verified change requires the full list.
12. If verified tracked data changed, monitor writes a pending WhatsApp event with a deterministic fingerprint.
13. The WhatsApp delivery job reads the event 15 minutes later, delivers it once if new, records delivery acknowledgement, and remains silent otherwise.

## 13. Acceptance criteria

1. Each scheduled run has at least 15 seconds between marketplace requests.
2. A single failed collection does not prevent the remaining collections from updating.
3. Failed rows retain last-known-good values and their last-successful timestamps.
4. Discord receives an update for every scheduled check, using active-only rows on verified no-change runs and the complete collection list when a verified update occurs.
5. WhatsApp receives no message unless a verified data change exists.
6. Active collection rows are visually prominent in both destinations.
7. Discord active rows have clickable masked View links without preview cards.
8. WhatsApp contains no View URLs.
9. Routine status contains no item name, token ID, token URL, contract ID, or raw source URL.
10. Last Exotic sales come from collection Activity, with product trait confirmation.
11. The current table and JSON state remain internally consistent.

## 14. Review and change management

This PRD is the maintained requirements reference. Review it whenever collections, schedules, notification policy, display requirements, marketplace logic, or failure handling changes. Update the version, last-updated timestamp, affected requirements, and acceptance criteria with each material change.
