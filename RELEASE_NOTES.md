<!-- release: v2.12.1338 -->

## What's Changed

**Configurable Tesla force-discharge tariff prices**

Tesla Powerwall setup and Tesla API connection settings now provide separate
buy and sell prices for the temporary tariff used during force discharge. The
fields are prefilled at $0.55/kWh buy and $25.00/kWh sell, based on a working
configuration reported by the community. Existing installations use these
defaults until changed. The sell price must exceed the buy price.

**Temporary rates stay separate from the normal tariff**

PowerSync applies the configured rates only within the timed Tesla
force-discharge window and continues restoring the saved tariff afterward.
Grid Charging, export permission, reserve, and command-readback safeguards
remain in place. Tesla ultimately controls physical Powerwall behavior, so a
tariff upload alone does not confirm battery export.

Update available via HACS
