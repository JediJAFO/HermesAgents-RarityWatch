# Source-only restore — NOT a disaster-recovery state backup

Keep every restored schedule disabled until private configuration and baseline validation are complete.
Install Python 3.11+, python-docx, psutil and tzdata; Node.js and Playwright (install under HERMES_HOME/hermes-agent so the existing absolute module reference resolves). Install Chrome separately. No package binaries or dependency lockfiles are included: versions must be validated on the target host.

Copy scripts/ and price-watches/ preserving their sibling layout. Materialize __HERMES_HOME__, __DOCUMENTS__, __USER_HOME__ in copied text files to forward-slash absolute target paths before execution. Do not run the templates in place. The production source uses fixed Windows paths; this explicit relocation step is required, including Node at HERMES_HOME/node/node.exe. Add Node to PATH for the WhatsApp wrapper. Review git repository paths separately before enabling backups.

Start a dedicated Chrome instance bound to loopback CDP port 9222, with a NEW monitor-only user-data directory. Never reuse a normal browser profile, expose CDP externally, or terminate user Chrome processes. Both collector and enrichment currently expect http://[::1]:9222. Browser startup/supervision and scheduler installation are deployment responsibilities, not bundled services.

Install Hermes independently. Recreate no-agent cron schedules from restore-config.json using supported Hermes scheduling controls, configure timezone America/New_York and destinations privately. Preserve/map the Exotic primary job ID in the two heartbeat-context scripts: they currently refer to the original ID. The executions.db schema must provide id, job_id, pid, status, scheduled_instant. Scheduler timeouts must exceed the collector budget plus preflight.

Supply private mcfarlane-exotics.json or mcfarlane-dc-sales.json in price-watches. Exact collection URLs/allowlists, historic baselines, dedupe and pending delivery records are deliberately NOT here. Supply mcfarlane-wallet-aliases.json (an empty exact_aliases/masked_aliases structure is acceptable) and mcfarlane-pol-usd-daily.json (empty rates object is acceptable for sales). RARIBLE_API_KEY is injected via the runtime secret facility, never this repository. A lost baseline requires a separately approved silent rebaseline before alerts; do not start against an empty production state or replay saved pending events.

No automatically restored live state or end-to-end marketplace/delivery claim is made. Offline verification checks source closure and syntax only. Existing backup repositories may contain legacy private artifacts: review/remove those before any push; this source snapshot does not sanitize git history.
