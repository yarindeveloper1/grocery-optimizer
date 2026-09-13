# Grocery Optimizer

Mobile grocery price optimizer for ZIP 20016.

Current stores:
- Wegmans Wisconsin Ave
- Giant Cathedral Commons
- ALDI localized to ZIP 20016

The app lives in `docs/`. A scheduled GitHub Action collects prices into `docs/prices.json` every morning.

The optimizer only uses prices that the collector successfully verifies. It does not substitute sample prices as live data.
