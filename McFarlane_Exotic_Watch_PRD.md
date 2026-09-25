# Product Requirements Document — McFarlane Exotic Watch

**Version:** 4.0
**Agent source audit:** 2026-09-16; local code and recurring configuration only, no human approval, live collection or delivery test implied.
**Status:** Active implementation; deployment and delivery caveats below.
**Private state:** HERMES_HOME/price-watches/mcfarlane-exotics.json (not backed up).

## Purpose and current scope

Monitor 23 preserved privately configured Exotic watches plus future Exotic or Legendary watches for verified BUY NOW listings with an exact product-page rarity trait. Each watch identity is exact normalized contract plus rarity, so one contract may be monitored independently for both rarities. Bids, purchases, wallet operations and payment UI are excluded. Historical Exotic sales remain independent of present availability. Legendary v1 covers active listings, independent baselines, candidates, alerts and retry state; it does not add Legendary historical-sale enrichment. The approved roster is private and must be supplied on restore; this document is not a collection inventory.

## Zero-LLM collection management

- `scripts/rarity_watch_manager.py` accepts JSON and implements Add, Disable, Remove and List Saved without an agent or model call. Polygon contracts must be exactly `0x` plus 40 hexadecimal characters and rarity must be exactly `Exotic` or `Legendary`.
- Add owns the shared execution lease, deduplicates exact contract+rarity, resolves exact metadata from the local full MTD catalog before an exact Rarible metadata fallback, then invokes the established validating Playwright collector for only that target. Add succeeds only after a verified baseline; failed or ambiguous checks do not save or enable a watch and never claim that nothing is available. Existing inventory becomes the initial baseline and never generates a listing-change alert. After the lease is released, a dedicated deterministic Discord helper posts and reads back one onboarding status in the configured Exotic channel: either verified active BUY NOW listings or an explicit no-active-listings result. A pending notice is persisted before delivery, and resubmitting the exact watch retries that notice without another marketplace check. A baseline-verified watch is enabled for subsequent interval and 8 AM heartbeat reports even if Discord delivery is temporarily pending. Disable, Remove and List Saved make no marketplace/API/browser call and send no notification.
- State schema v2 adds `rarity`, `watch_key` and `enabled` to each collection. Legacy records migrate to Exotic while preserving baselines, history, observations and delivery state. Legacy candidate keys migrate to exact watch keys. Disable retains saved history; Remove deletes only the exact watch and its exact pending candidate/retry target references.
- The unified `plugins/rarity-watch-manager` package provides a persistent Hermes Desktop pane backed by `ctx.rest` and the scoped Python plugin API. It never submits a prompt. The pane visibly labels Add as live and provides Add, Disable, Remove and List Saved controls with an in-pane Result region.
- `scripts/rarity_watch_portal.py` provides the same contract/rarity/optional display/category form as a standalone loopback browser portal on port 8767. It calls `rarity_watch_manager.py` directly—never a plugin REST route, Hermes agent, prompt, or coding model—so portal operation consumes zero LLM tokens. Add visibly reports Discord delivery as read-back verified or pending; Disable, Remove, and List Saved are identified as saved-state-only. The portal enforces exact loopback Host and Origin, a private persistent CSRF secret that remains valid across server restarts, strict request sizing, exact action/contract/rarity validation, no-store responses, a restrictive CSP, and escaped output. `/health` reports `llm_tokens: 0`.

## Actual runtime and cadence

- Hermes no-agent primary run_mcfarlane_exotic_deterministic.py owns a shared execution lease, refreshes current POL/USD, then invokes the Node collector run_mcfarlane_exotic_deterministic.js. Python psutil and the read-only cron execution ledger identify the actual scheduled occurrence.
- Primary schedule: 00:00, 08:00, 12:00, 16:00 and 20:00 America/New_York. WhatsApp downstream: minute 15 of those hours. Priority confirmation: minute 14, not a guaranteed ten minutes after run completion; it can defer while the lease is busy.
- Technical retry scheduler polls each minute; purchase-priority confirmation reads saved candidates first. Empty/not-due stages make no marketplace request.
- Silent missing-history enrichment polls every 15 minutes, yields to primary/priority/retry work and uses the same lease.
- Browser collection uses Playwright attached to a dedicated Chrome CDP endpoint on IPv6 loopback port 9222. Browser provisioning/supervision is external to the collector. The source does not prove an automatic browser restart service exists.

## Collection, verification and comparison

- Collection navigation is serial, canonical rarity-filtered URLs are checked and BUY NOW cards are verified on token pages for the watch's exact Exotic or Legendary trait. Bid-only cards do not become listings. A missing/ambiguous control or loading shell is not proof of an empty collection.
- The installed collector uses GAP_MS=10000, a 4-second load settle and two collection attempts. This is an implementation fact, not compliance with the saved operating preference for at least 15 seconds. Closing that pacing mismatch requires a separate runtime change.
- Batch budget is min(1500, max(600, selected collection count × 60 + 120)) seconds; wrapper allowance adds 120 seconds, with preflight/rate-refresh outside that allowance. Scheduler timeout must allow the entire wrapper.
- Explicit global blocking evidence stops work. Local failures and budget-skipped collections retain last-known-good rows. Checkpoints preserve verified progress. The collector creates and closes its own page; it does not enumerate or close unrelated tabs. One bounded owned-page reconnect is permitted within the remaining deadline. Replaced-page cleanup is not guaranteed by the inspected recovery branch.
- Differences are held as candidates until an independent matching observation promotes them. Candidate observations never supply confirmed removals. New token identity or a same-token/same-currency price cut of at least 10% is purchase-priority. Smaller decreases, removals and increases wait for ordinary confirmation.
- Active seller enrichment uses active order makers and the canonical private `mcfarlane-wallet-aliases.json` database shared with completed-sales and tracked-wallet monitors. Listing set time is separate from collection Activity sale time. Metadata-only, seller-only and valuation changes are not listing-change events. The saved-state-only recurring-wallet audit compares distinct listing/sale/activity occurrences against that shared alias database without marketplace or notification calls.

## Reliability and recovery

- Python and Node use a shared filesystem lease with inherited owner-token validation. Busy means defer; it must not spend retry budget. Stale leases fail closed and need owner/process inspection rather than blind deletion.
- Technical recovery targets unresolved failures only, prioritizing budget-skipped names. Five retry stages use delays 1, 3, 5, 10 and 20 minutes, scheduled relative to each preceding outcome; they are not five absolute offsets from the original failure. Exhaustion is tied to primary generation and cannot be replenished by polling the same generation.
- A primary partial result is reported once; intermediate incomplete targeted passes are suppressed. The unresolved set and confirmed undelivered deltas survive targeted passes; full recovery emits a resolved report.
- Owed 08:00 heartbeat support now exists: wrapper arms pending8am before collection, Node claims it only after complete recovery and sufficiently fresh observations, and deliver_exotic_owed_heartbeat.py offers a saved-only handoff/status/verified-ledger repair helper. No recurring job for that helper was present in the inspected schedules; normal targeted recovery can claim the debt itself.
- Heartbeat state records generation/handoff before the scheduler's actual Discord send. It is NOT a delivery acknowledgment: a downstream failure can lose the automatic replay opportunity. Runtime support and offline tests are not proof of a live delivered recovery heartbeat. Ledger schema, preserved primary job ID and correct process ancestry are prerequisites.
- At this review's offline regression run, 24 of 25 existing Python reliability/context/enrichment/owed-heartbeat tests passed. The owed-heartbeat seed test failed because required_observation_at was absent instead of the ledger's actual started_at. This is a specific unresolved repair-path freshness gap, not proof that all heartbeat recovery works; rerun after a targeted runtime fix.

## Destination-specific output

- Ordinary Discord no-change primary results are compact status only, not unchanged collection rows. Confirmed changes contain details only for affected collections and explicit removals, including partial removals.
- The actual scheduled primary 08:00 ET occurrence is the compact active-listings exception: one concise collection/price line per active listing, explicitly labeled Exotic or Legendary and sorted by numeric selling price from lowest to highest, with POL and USD plus seller Name Tag or the saved four-character wallet suffix. Manual runs at 08:00 do not qualify merely because of wall time. If owed, it can accompany the eventual resolved output; already-handed-off dates are not replayed automatically.
- WhatsApp uses the separate exotic_whatsapp_policy.js and saved-only downstream script. Only independently confirmed new listings and same-token/same-currency decreases of any size are eligible. No removals, increases, unchanged inventory, currency-only or metadata-only changes. The 10% threshold controls urgency, not final WhatsApp eligibility.
- Downstream stdout is handed to Hermes delivery; consuming a queue is not proof the destination received it. Saved review/preview tools are not part of ordinary delta output.

## Valuation and historical enrichment

- Current POL/USD is refreshed before collection; source failure retains the saved rate and does not intentionally block the collector. Current listing USD is display-only.
- Last Exotic sale is selected from completed Activity and independently verified on a rendered product trait page. The resumable missing-history worker excludes user-disabled/terminal searches, persists pagination/progress, paces requests and cools down failures. Missing or exhausted history means not verified, never proof of no sale.
- Saved sale-time priceUsd/amountUsd or historical cached valuation remains historical; do not reprice old sales using current POL spot.

## Backup, restore and verification boundary

The explicit monitor_backup_manifest.json includes the Python wrapper, Node collector and dynamically required WhatsApp policy, shared lease, rarity manager CLI/API/Desktop plugin, standalone zero-token loopback portal and their offline tests, technical retries, priority confirmation, spot-rate refresh, missing-history worker, alias resolver code, owed-heartbeat helper and backup/PRD utilities. Only code and these PRDs enter the source snapshot. No live state, raw aliases, wallets, private URLs, destinations, credentials, logs or browser profiles are copied. Python/Node packages are declared dependencies, not bundled binaries.

RESTORE.md and restore-config.json describe relocation placeholders, required private configuration, disabled schedule templates, browser/Node installation and controlled rebaseline. This is not a complete stateful disaster-recovery backup: previous dedupe, pending delivery, exact roster and history require separate private recovery. Legacy repository data/history is not retroactively sanitized.

Automated refresh fingerprints every manifest source plus stable relevant recurring schedule semantics, private roster identity and allowlisted declarative policies. Declarations may themselves lag code; hashing them does not certify compliance. Observation timestamps, results and delivery queues do not create drift. It never advances a human review date merely because hashes changed. Markdown content changes regenerate DOCX. Daily backup checks a per-ET-day success/attempt cap before repository mutation, detects identical snapshots, refuses dirty repositories and legacy data artifacts, stages explicit files only and verifies the remote commit after a push. Original scheduler IDs are replaced with private restore placeholders; snapshot verification rejects extra files and scans DOCX package text, metadata and external relationships. Local snapshot validation does not claim a cloud update.

The enabled daily backup remains at 23:40 America/New_York. Offline readiness is not permission to run a monitor, deliver notifications or push. Existing repository history and legacy non-manifest files require separate review; source-only snapshot validation covers the new snapshot, not historical commits.

<!-- automated-drift:start -->
**Observed implementation fingerprint:** `e9567384c8451a48698190bd715eba03913cf350512cc5ba17dcc36a6b2a4328`
**Automated drift status:** changed or unreviewed; substantive review required. Hash comparison is not a requirements review.
<!-- automated-drift:end -->
