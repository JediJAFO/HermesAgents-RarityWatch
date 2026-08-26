# HermesAgents — sanitized backup

This repository contains presentation/rendering code and product requirements for the McFarlane Exotic Watch.

## Deliberately excluded

- live monitor state (`mcfarlane-exotics.json`), which contains private verification data and marketplace identifiers;
- Hermes credentials, `.env` files, gateway configuration, and delivery destinations;
- logs, browser workspaces, and cron output history.

## Restoring

Copy the renderer scripts to the local Hermes `price-watches` directory. Recreate state and credentials locally; they are intentionally not backed up here.
