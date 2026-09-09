# Product Requirements Document — McFarlane Exotic Watch

**Version:** 2.0  
**Last updated:** 2026-09-08 EDT  
**Implementation signature:** `1d39d262b278`  
**Status:** Active, deterministic implementation  
**Private state:** `C:/Users/jltfo/AppData/Local/hermes/price-watches/mcfarlane-exotics.json`

## Purpose

Monitor the configured McFarlane Toys Digital collections for active **BUY NOW** listings whose product-page trait is **Rarity: Exotic**. Preserve verified listings and historical Exotic-sale context, publish an every-run Discord report, and send WhatsApp only for verified listing changes.

## Scope and safeguards

- Tracks the ten configured collections held privately in the state file.
- Counts BUY NOW listings only; bids, wallet actions, purchases, and payment UI are out of scope.
- Runs at 00:00, 08:00, 12:00, 16:00, and 20:00 Eastern; WhatsApp delivery is scheduled 15 minutes after each main interval.
- Uses a dedicated, isolated headless Chrome profile only—not the user's normal Chrome profile.
- Navigates serially with at least a 10-second gap between every marketplace navigation, including product-trait checks.
- Stops marketplace-wide work only for explicit 429, CAPTCHA/challenge, or access-denied evidence. Redirects, loading shells, missing controls, and timeouts are collection-local failures.

## Collection verification workflow

1. Connect to the dedicated Chrome DevTools endpoint.
2. Close abandoned pages in the dedicated monitor profile before beginning a run.
3. For each saved filtered collection URL, verify the canonical URL retains its requested Exotic and in-stock filter parameters.
4. Extract only cards clearly marked BUY NOW.
5. For every active candidate, open its product page and require a rendered `Rarity: Exotic` trait before accepting it.
6. Normalize listings by token identity, price, currency, and stable private name. Presentation-only card text changes must not create an alert.
7. Retain last-known-good rows on unavailable/partial results and continue with the other collections.
8. Enforce a bounded run budget. If insufficient time remains, return a safe partial result rather than timing out.

## Change confirmation and state

- A newly observed listing, disappearance, count change, token change, price change, or currency change is first held as a candidate.
- The same normalized difference must appear in the next independent interval before it becomes a verified change.
- Candidate or failed observations never overwrite a verified baseline and never create a WhatsApp event.
- Verified changes create a deduplicated pending WhatsApp event; delivery records its fingerprint so it cannot send twice.
- The collection Activity view remains authoritative for the saved last Exotic-sale price/time. Product pages verify the trait only.

## Notifications

### Discord

Discord receives every primary interval report.

- No verified change: active listings only.
- Verified change: complete collection status.
- Partial run: identifies unavailable collections and confirms their last-known-good data was retained.
- Public formatting uses `POL`, green emphasis for active rows, `last:` for historical sales, and masked no-preview View links.

### WhatsApp

WhatsApp receives only a verified, undelivered change event.

- No change, candidate-only result, or failure-only result returns `[SILENT]`.
- Messages contain no URLs, token IDs, contract IDs, or NFT item names.
- Active rows use `🟢 YES`, `POL`, and `Last:`.

## Reliability requirements

1. Dedicated Chrome is reset independently of the user's browser when its monitor-only profile becomes unhealthy.
2. Every run removes stale monitor tabs before collection work.
3. Connection, navigation, loading, product-trait, and budget failures retain state and report partial status rather than producing a false change.
4. No change can be promoted or delivered unless it survives independent-interval confirmation.
5. The monitor's Discord and WhatsApp delivery stages are deterministic no-agent scripts; browser collection does not require an LLM runtime.

## Backup and review

The sanitized Exotic backup includes this Markdown PRD and its DOCX counterpart. Before the daily Exotic backup evaluates changes, the PRD refresh step reviews the current deterministic implementation and only rewrites the PRD when the tracked implementation signature changed. Private live state, credentials, logs, token identifiers, and source URLs are excluded from the backup.
