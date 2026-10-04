# Changelog

## 0.7.0

- Payback: optional yearly panel ageing, battery ageing and price change, shown as a second
  break-even date and chart line beside the plain estimate.
- Payback: savings are split into what the panels alone would save and what the battery adds.

## 0.6.0

- Payback: enter what the system cost to see how much has been saved, the share paid
  back, an estimated break-even date and a chart of progress.

## 0.5.0

- Compare tariffs: see what your real usage would have cost on other tariffs, or on your
  own tariff after a price change.
- Tariffs to compare can have several time windows.
- Look up Octopus Energy's published prices to fill in a tariff.

## 0.4.0

- Readings older than `keep_years` (default 10) are now deleted, so storage has a ceiling.
- The dashboard shows how much space the stored readings take.

## 0.3.2

- Documentation: how to get an hourly export from Sigen AI for the history import.

## 0.3.1

- A credit now shows as -£12.97 instead of £-12.97.

## 0.3.0

- First public release.
- Tiles, the by-device section and the breakdown chart are left out when the EV charger or
  smart load sensors are not in Home Assistant.
- A notice is shown if no Sigenergy sensors are found.
- New options: `smart_load_label` and `ev_on_smart_load`.
