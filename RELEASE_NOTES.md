<!-- release: v2.12.1336 -->

## What's Changed

- Fix EV demand arbitration when Smart Schedule and Price-Level Charging are
  evaluated together. A blocked or empty Smart Schedule result could clear
  unrelated Price-Level EV demand from the battery optimizer's load forecast.
- Scope overlay suppression to the physical EV loadpoint owned by the
  co-optimized or blocked Smart Schedule plan, while preserving independent
  EV loadpoints and the existing external planned-load authority.
- Add regressions for empty Smart plans, blocked loadpoint ownership,
  co-optimized loadpoints, and canonical loadpoint merging.

Update available via HACS.
