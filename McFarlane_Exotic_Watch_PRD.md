# Product Requirements Document — McFarlane Rarity Watch

**Compatibility filename:** `McFarlane_Exotic_Watch_PRD.md`  
**Review date:** 2026-10-22  
**Status:** Active deterministic implementation with current backup defects.  
**Verification boundary:** Reviewed local source, private state aggregates, `jobs.json`, and local execution records. Offline Python tests and the bundled-Node collector test were run. No marketplace request, browser collection, Discord/WhatsApp post, backup push, or live delivery read-back was performed for this review.

## 1. Purpose and current scope

The compatibility name understates the implementation. The monitor currently tracks exact Polygon contract-plus-rarity watches for **Exotic, Legendary, Epic, and Rare** assets. The private saved roster contained **46 enabled watches** at review: **22 Exotic, 12 Legendary, 9 Epic, and 3 Rare**. These counts were calculated from current state; this document intentionally omits contract addresses, product inventory, aliases, destination identifiers, and marketplace URLs.

The monitor detects active BUY NOW listings only after the collection result and product page establish the configured rarity. It does not buy, bid, connect a wallet, or infer rarity from a collection name. Existing inventory is baselined without a change alert.

## 2. Runtime architecture

- The scheduled Python wrapper owns the shared execution lease and invokes the Node/Playwright collector attached to a dedicated local browser endpoint.
- The deterministic manager and loopback portal support Add, Disable, Remove, List, full update, pending-candidate confirmation, saved-heartbeat resend, and status inspection without an agent/model call.
- Current manager validation allows the four implemented rarities. Each watch is identified by normalized contract plus rarity.
- The collector is serial. It uses a 10-second navigation gap, a 4-second render settle, and at most two measured attempts per collection observation.
- Batch execution is bounded from 600 to 1,500 seconds according to selected-watch count, with additional wrapper allowance.
- Last-known-good collection baselines survive collection-local failures and budget skips. A global marketplace/browser block stops the run and leaves all baselines unchanged.

## 3. Confirmed-change semantics

A changed collection snapshot becomes a pending candidate. A second matching observation is required before promotion. The current Discord path can report listing additions, removals, and repricing represented by the promoted snapshot; the separate WhatsApp completion path is narrower.

The active WhatsApp job runs saved-state processing only. Current policy permits **confirmed new listings only**, and only for Exotic or Legendary watches. It excludes Epic/Rare, removals, price changes, stale queued offers, and offers no longer present in the verified baseline. This is narrower than older documentation that described lower-price WhatsApp alerts.

Seller labels resolve through the shared private alias database when possible; otherwise only a masked suffix is displayed. Full wallets remain private in state.

## 4. Authoritative schedules

All enabled cron expressions below were read from current `jobs.json`. No relevant job has an explicit per-job timezone field; intended interpretation is America/New_York, but scheduler daylight-saving behavior is not proven by configuration alone.

| Function | Current schedule |
| --- | --- |
| Full rarity collection | 01:00, 09:00, 13:00, 17:00, 21:00 daily |
| Purchase-priority/pending confirmation | Every 2 minutes |
| Bounded technical retry poll | Every minute |
| WhatsApp confirmed-new-listing consumer | Every 2 minutes on odd minutes |
| Daily source backup attempt | 23:40 |

The daily active-listings heartbeat is tied to the actual **09:00 Eastern scheduled occurrence**, not 08:00. A busy full run persists a deferred-primary obligation; a missed 09:00 occurrence is preserved over later busy occurrences. Heartbeat debt is claimed only after a complete sufficiently fresh observation.

## 5. State, retention, and privacy

Private state stores the roster, current verified baselines, pending candidates, retry generations, onboarding notices, current valuation context, heartbeat debt, and pending delivery data. It is not a public collection inventory.

Retention is mixed rather than governed by one policy:

- Block diagnostics are pruned to the newest **20** records.
- Current listing baselines and per-watch historical enrichment remain in the main state without a documented age-based purge.
- Pending delivery/retry/onboarding records remain until their code path resolves or explicitly removes them.
- Browser profile data, logs, credentials, aliases, wallets, destination IDs, and state are private and excluded from source snapshots.

Any future retention change must preserve unresolved delivery and heartbeat obligations before deleting historical records.

## 6. Failure and recovery behavior

- Lease contention defers work and does not consume retry budget. Stale lease recovery requires process-owner validation; age alone is insufficient.
- Local collection failure retains that watch’s prior baseline. A complete-success heartbeat is withheld when any required watch is unresolved.
- Technical retries target unresolved watches in bounded stages. A recurring one-minute poll does not mean a marketplace call occurs every minute.
- Browser/CDP unavailability produces a shared failure summary and keeps last-known-good data.
- Delivery ledgers and exact read-back helpers reduce duplicates but do not prove unconditional exactly-once delivery across unknown network outcomes.
- The scheduler’s `completed` status establishes process completion only; it does not independently prove collection completeness or destination receipt.

## 7. Current operational errors and backup gaps

As of the review date, the latest primary and frequent support jobs had completed ledger records, but the daily Exotic backup has an open failure. The current scheduler environment cannot import the DOCX library used by the PRD refresher. This review did not repair it because DOCX generation and runtime changes were explicitly out of scope.

The source-only backup manifest also omits a local browser-recovery module imported by the retry path. The backup import-closure test therefore failed. The bundled Node runtime passed the collector tests; the shell-default Node runtime was too old for the installed Playwright version. Scheduled code should continue to use the configured bundled runtime rather than assuming `node` on PATH is sufficient.

The source backup intentionally excludes all live state. Even after the manifest and dependency defects are fixed, source backup alone cannot restore roster identity, baselines, dedupe, pending alerts, heartbeat debt, aliases, browser profile, credentials, or destination mapping.

## 8. Acceptance and verification boundaries

Offline review verified the 46-watch rarity totals programmatically, the five primary daily occurrences and support schedules from `jobs.json`, Python reliability/portal/manager tests, and the Node collector test using the bundled runtime. It did **not** verify present marketplace contents, browser health, provider completeness, current destination permissions, any actual message, or any remote backup commit.

A future operational sign-off requires: a complete scheduled collection, heartbeat/delta delivery read-back, scheduler timezone/DST evidence, corrected backup dependency/import closure, and a verified private-state recovery procedure.

<!-- automated-drift:start -->
**Observed implementation fingerprint:** `3c609b3c538c372a6c30e78b0048c3d2e816669ae7eea025d5294d026401218c`
**Automated drift status:** changed or unreviewed; substantive review required. Hash comparison is not a requirements review.
<!-- automated-drift:end -->
