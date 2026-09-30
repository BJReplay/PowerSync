> [!WARNING]
> **Disclaimer:** This is an unofficial integration and is not affiliated with or endorsed by Tesla. Use at your own risk.

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

[![Add Repository to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=BJReplay&repository=Tesla-v1r&category=integration)

Or manually:

1. Open HACS > three dots > Custom repositories
2. Add `https://github.com/BHReplay/Tesla-v1r` (Category: Integration)
3. Download Tesla-v1r and restart Home Assistant
4. Settings > Devices & Services > Add Integration > "Tesla v1r"
5. Follow the guided setup

---

## Features

| Feature | Description | Wiki |
|---------|-------------|------|
| **Battery System Setup** | Tesla setup guides | [Setup Guide](https://github.com/BJReplay/Tesla-v1r/wiki/Battery-System-Setup) |
| **Sensors** | Core power sensors | [Full List](https://github.com/BJReplay/Tesla-v1r/wiki/Sensors) |
| **Services** | Force charge/discharge, hold SOC, backup reserve, **off-grid/reconnect** | [Reference](https://github.com/BJReplay/Tesla-v1r/wiki/Services-Reference) |
| **Troubleshooting** | Connection issues, debug logging, common fixes | [Guide](https://github.com/BJReplay/Tesla-v1r/wiki/Troubleshooting) |


---

## Sponsors

<!-- sponsors --><!-- sponsors -->

## Support

- **Discussions:** https://github.com/BJReplay/Tesla-v1r/discussions — bug reports, feature requests, and support
- **Bugs / Issues:** https://github.com/BJReplay/Tesla-v1r/issues — genuine issues when you've found a real bug
- **Wiki:** https://github.com/BJReplay/Tesla-v1r/wiki

## License

                             Apache License
                       Version 2.0, January 2004
                    http://www.apache.org/licenses/

## Credits

Based on the work of Ben Boller [@Bolagnaise](https://github.com/Bolagnaise/PowerSync)
