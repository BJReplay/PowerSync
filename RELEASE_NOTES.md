<!-- release: v2.12.1337 -->

## What's Changed

**Smart Schedule respects EPEX price intervals**

Generic Charger Smart Schedule now bounds each EPEX charging window to the
actual timestamped source interval instead of treating every price row as a
full hour. This prevents a short cheap-price slot from overstating available
charging energy or reporting an achievable departure target when the permitted
time cannot deliver it.

**EPEX windows remain executable and non-overlapping**

Distinct EPEX slots can contribute independently, including the final slot
when only its fixed five-minute optimizer interval is available. Deadline
truncation and configured price limits continue to apply to the resulting plan.

Update available via HACS
