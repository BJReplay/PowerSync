> [!WARNING] **Disclaimer:** This is an unofficial integration and is not affiliated with or endorsed by Tesla. Use at your own risk.

---

Forked from the magnificent work done by Ben Boller at [@Bolagnaise](https://github.com/Bolagnaise/PowerSync) - see the source for the sources he built on.

I've forked it for the Powerwall v1r pairing, and control only.

This represents a basic replacement of Telsa Fleet and the original Tesla integrations.

## Supported Systems

Battery controls for Tesla powerwall via v1r.

### Battery Systems

| System | Connection | Control |
|--------|-----------|---------|
| **Tesla Powerwall** | Fleet API / v1r | force charge/discharge, **off-grid/reconnect** |

---

## Quick Start

1. **Install** via [HACS](#installation) (custom repository)
2. **Add Integration** — Settings > Devices & Services > Add Integration > "Tesla v1r"
3. **Done!** Sensors and controls appear automatically that match those that you would have previous seen using the Tesla or Tesla Fleet integraions to the extent that they are available.  Update existing dashboards and automations to use these new sensors and controls, and when you're happy that they're working, remove the old integrations..

---

## Installation

### Prerequisites

- Home Assistant with [HACS](https://hacs.xyz/) installed
- A Tesla Powerwall 2 or Powerwall 3 battery system with network access

### Steps

[![Add Repository to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=BJReplay&repository=PowerSync&category=integration)

Or manually:

1. Open HACS > three dots > Custom repositories
2. Add `https://github.com/BHReplay/PowerSync` (Category: Integration)
3. Download PowerSync and restart Home Assistant
4. Settings > Devices & Services > Add Integration > "PowerSync"
5. Follow the guided setup for your provider and battery system

---

## Features

| Feature | Description | Wiki |
|---------|-------------|------|
| **Battery System Setup** | Tesla, FoxESS, Sigenergy, GoodWe, Sungrow, AlphaESS, ESY Sunhome, Solax Hybrid, SAJ H2/HS2, Fronius GEN24 storage, SolarEdge, Anker Solix, and custom/external controller setup guides | [Setup Guide](https://github.com/bolagnaise/PowerSync/wiki/Battery-System-Setup) |
| **Smart Optimization** | Built-in LP optimizer calculates optimal charge/discharge schedule using prices, solar, and load. Optional controls include Profit Max, Charge By Time, auto-applied forecast reserve, selected household load history, planned EV load, and measured quota settlement for GloBird ZeroHero/ZeroCharge and CovaU SolarMax. **Solar forecasting via Solcast, Open-Meteo Solar Forecast, or Volcast must be configured for accurate scheduling.** Optional [AI Plan Explanation provider eligibility and billing guidance](https://github.com/bolagnaise/PowerSync/wiki/Smart-Optimization#ai-plan-explanation) is available in the Smart Optimization guide. | [Details](https://github.com/bolagnaise/PowerSync/wiki/Smart-Optimization) |
| **Flexible Exports** | Reads a fail-closed operating envelope from separately certified site equipment exposed in Home Assistant. This release is monitoring-only while the required SAPN site soak is completed; active enforcement remains release-gated. PowerSync is not a CSIP-AUS client and provides no network-limit override. | [Details](https://github.com/bolagnaise/PowerSync/wiki/Smart-Optimization#network-export-limits--flexible-exports) |
| **EV Smart Charging** | Coordinate EV charging with battery optimization — Solar, Cheapest, Deadline modes | [Details](https://github.com/bolagnaise/PowerSync/wiki/EV-Smart-Charging) |
| **Advanced Features** | AEMO spike detection, solar curtailment, spike protection, export boost, **off-grid control** | [Details](https://github.com/bolagnaise/PowerSync/wiki/Advanced-Features) |
| **Sensors** | Core power sensors, daily energy tracking, FoxESS Modbus sensors, optimizer status | [Full List](https://github.com/bolagnaise/PowerSync/wiki/Sensors) |
| **Services** | Force charge/discharge, hold SOC, TOU sync, backup reserve, inverter curtailment, **off-grid/reconnect** | [Reference](https://github.com/bolagnaise/PowerSync/wiki/Services-Reference) |
| **Troubleshooting** | Connection issues, debug logging, common fixes | [Guide](https://github.com/bolagnaise/PowerSync/wiki/Troubleshooting) |

### Custom tariff daily supply charge

For a static custom tariff uploaded to Tesla, `daily_supply_charge` is an
optional decimal amount in the tariff currency **per day**. Set it when your
contracted supply charge differs from the selected tariff template. If it is
omitted, PowerSync uses that template's daily supply charge when available;
tariffs without either value keep their existing no-amount daily-charge row.

---

## Mobile App

Remote monitoring and control via iOS and Android.

**iOS:** [Join TestFlight](https://testflight.apple.com/join/FhnUtSFy) | **Android:** [Google Play](https://play.google.com/store/apps/details?id=com.powersync.mobile)

### Setup

1. Get your Home Assistant URL (local or Nabu Casa)
2. Create a **Long-Lived Access Token** in your HA profile
3. Enter URL + token in the app

### Features

- **Dashboard** — Live pricing, power flow, energy summary
- **Controls** — Force charge/discharge, backup reserve, off-grid/reconnect
- **Smart Optimization** — 24-hour battery schedule, action plan, cost tracking, Profit Max, and Charge By Time
- **EV Charging** — Smart scheduling, solar surplus, price-level charging
- **Automations** — Time, price, and grid-status triggers with battery/EV/grid actions
- **Settings** — Battery, EV, provider, and optimization configuration
- **Demo Mode** — Try the app without a Home Assistant connection using simulated data

<p align="center">
  <img src="docs/images/app-hero.png" alt="Dashboard — live energy flow" width="200"/>
  <img src="docs/images/app-optimization.png" alt="Smart Optimization summary" width="200"/>
  <img src="docs/images/app-action-plan.png" alt="24-hour LP action plan" width="200"/>
</p>
<p align="center">
  <img src="docs/images/app-price-chart.png" alt="TOU schedule and price forecast" width="200"/>
  <img src="docs/images/app-ev-charging.png" alt="EV Charging" width="200"/>
  <img src="docs/images/app-settings.png" alt="Settings" width="200"/>
</p>

---

## Sponsors

<!-- sponsors --><a href="https://github.com/barry-heap"><img src="https:&#x2F;&#x2F;github.com&#x2F;barry-heap.png" width="60px" alt="User avatar: Barry Heap" /></a><a href="https://github.com/richardkeit"><img src="https:&#x2F;&#x2F;github.com&#x2F;richardkeit.png" width="60px" alt="User avatar: Richard Keit" /></a><a href="https://github.com/drsamking86-coder"><img src="https:&#x2F;&#x2F;github.com&#x2F;drsamking86-coder.png" width="60px" alt="User avatar: " /></a><a href="https://github.com/JoelyMoley"><img src="https:&#x2F;&#x2F;github.com&#x2F;JoelyMoley.png" width="60px" alt="User avatar: " /></a><a href="https://github.com/sgdodds"><img src="https:&#x2F;&#x2F;github.com&#x2F;sgdodds.png" width="60px" alt="User avatar: " /></a><a href="https://github.com/philsweetnam"><img src="https:&#x2F;&#x2F;github.com&#x2F;philsweetnam.png" width="60px" alt="User avatar: PhilS" /></a><a href="https://github.com/Barbars11"><img src="https:&#x2F;&#x2F;github.com&#x2F;Barbars11.png" width="60px" alt="User avatar: " /></a><a href="https://github.com/Teslemetry"><img src="https:&#x2F;&#x2F;github.com&#x2F;Teslemetry.png" width="60px" alt="User avatar: Teslemetry.com" /></a><a href="https://github.com/zhenya-y"><img src="https:&#x2F;&#x2F;github.com&#x2F;zhenya-y.png" width="60px" alt="User avatar: " /></a><a href="https://github.com/rpcai"><img src="https:&#x2F;&#x2F;github.com&#x2F;rpcai.png" width="60px" alt="User avatar: " /></a><a href="https://github.com/greiginsydney"><img src="https:&#x2F;&#x2F;github.com&#x2F;greiginsydney.png" width="60px" alt="User avatar: Greig Sheridan" /></a><a href="https://github.com/Steve-gnome"><img src="https:&#x2F;&#x2F;github.com&#x2F;Steve-gnome.png" width="60px" alt="User avatar: steve" /></a><a href="https://github.com/upperdarkness"><img src="https:&#x2F;&#x2F;github.com&#x2F;upperdarkness.png" width="60px" alt="User avatar: " /></a><a href="https://github.com/timothyarnold1982"><img src="https:&#x2F;&#x2F;github.com&#x2F;timothyarnold1982.png" width="60px" alt="User avatar: " /></a><a href="https://github.com/xlrate76"><img src="https:&#x2F;&#x2F;github.com&#x2F;xlrate76.png" width="60px" alt="User avatar: " /></a><a href="https://github.com/JoshFAccord"><img src="https:&#x2F;&#x2F;github.com&#x2F;JoshFAccord.png" width="60px" alt="User avatar: " /></a><a href="https://github.com/Muleo14"><img src="https:&#x2F;&#x2F;github.com&#x2F;Muleo14.png" width="60px" alt="User avatar: " /></a><a href="https://github.com/hornet77al"><img src="https:&#x2F;&#x2F;github.com&#x2F;hornet77al.png" width="60px" alt="User avatar: " /></a><a href="https://github.com/ByteSizeFlash"><img src="https:&#x2F;&#x2F;github.com&#x2F;ByteSizeFlash.png" width="60px" alt="User avatar: Darren" /></a><a href="https://github.com/mattkellaway"><img src="https:&#x2F;&#x2F;github.com&#x2F;mattkellaway.png" width="60px" alt="User avatar: Matt" /></a><a href="https://github.com/nitro182"><img src="https:&#x2F;&#x2F;github.com&#x2F;nitro182.png" width="60px" alt="User avatar: " /></a><a href="https://github.com/permezel"><img src="https:&#x2F;&#x2F;github.com&#x2F;permezel.png" width="60px" alt="User avatar: " /></a><a href="https://github.com/skandiah94"><img src="https:&#x2F;&#x2F;github.com&#x2F;skandiah94.png" width="60px" alt="User avatar: " /></a><a href="https://github.com/tyler-gei"><img src="https:&#x2F;&#x2F;github.com&#x2F;tyler-gei.png" width="60px" alt="User avatar: " /></a><a href="https://github.com/Philo21"><img src="https:&#x2F;&#x2F;github.com&#x2F;Philo21.png" width="60px" alt="User avatar: Phillip" /></a><a href="https://github.com/GregoryDC"><img src="https:&#x2F;&#x2F;github.com&#x2F;GregoryDC.png" width="60px" alt="User avatar: " /></a><!-- sponsors -->

## Support

- **Discord:** https://discord.gg/eaWDWxEWE3 — bug reports, feature requests, and support
- **Wiki:** https://github.com/bolagnaise/PowerSync/wiki

## License

Copyright (c) 2024–2026 Ben Boller. All rights reserved.

Licensed under [PolyForm Noncommercial 1.0.0](LICENSE) — free for personal, hobby, and noncommercial use.

**Commercial use is prohibited without prior written permission from the copyright holder.** This includes use within a commercial organisation, integration into a paid product or service, and redistribution as part of a commercial system. To enquire about a commercial licence, contact [dev@powersync.cc](mailto:dev@powersync.cc).
