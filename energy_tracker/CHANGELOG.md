# Changelog

## 0.23.0

- Return on investment: a second savings line, and a tile, worked out from your bills
  wherever you have them, to set beside the tracker's own costing.

## 0.22.2

- The Battery health section has been removed.

## 0.22.1

- Alerts have been removed. The app is back to only ever reading from Home Assistant.

## 0.22.0

- The colours of Sigenergy's app are now used throughout: solar yellow, consumption purple,
  grid lilac-blue and battery cyan, on the tiles, the history chart and the consumption
  breakdown as well as the Power Metrics Chart.
- Each tile has a stripe in the colour its figure has on the charts.

## 0.21.0

- Yearly report: a calendar year on one page, beside the year before, with a line for each
  month and a print button.
- Backup and restore: download everything you have entered as one file, and restore it
  here or on another install.

## 0.20.2

- Alerts: removed the "no readings for an hour" alert.

## 0.20.1

- Alerts: removed the "no solar by midday" and "battery capacity dropped" alerts. Battery
  health is still shown on the page.

## 0.20.0

- Alerts: a notification through Home Assistant, to your phone and into Home Assistant's
  own notifications, when the battery does not charge in the cheap
  period, or too much is bought at the dearer rate. Off until you turn it on.
- Battery health: usable capacity, efficiency and full cycles, with the trend month by
  month.

## 0.19.0

- Tariff switch planner: the last twelve months on another tariff, month by month, beside
  what you paid.
- Charging plan: when the battery and the car would charge on that tariff, and how often
  the battery would run out before its next cheap time.
- Rate reminders: give a fixed-price deal its end date and the page reminds you a month
  before; it also says when half-hourly prices could not be fetched.

## 0.18.0

- Payback is now called Return on investment.
- A progress bar shows how much of the cost has been paid back and the amount remaining.
- The chart runs on past break-even (20 years after install unless changed) and shades the
  profit above the system cost, with a tile for the projected profit at the end.

## 0.17.3

- Consumption breakdown chart: point at it to see each part's power at that moment.

## 0.17.2

- Power Metrics Chart: "Total consumption" is now "Consumption".

## 0.17.1

- Power Metrics Chart: point at it to mark a moment and see every line's exact value then.

## 0.17.0

Faster to open, and lighter while open.

- The long-range figures (costs by year, payback, comparison, performance, device costs,
  monthly summary, bill check) are worked out in the background and kept ready, so the
  page no longer waits for them.
- The tiles at the top appear first, before the heavier sections are asked for.
- A folded section is not fetched until it is opened.
- The power charts refresh every minute and the rates and yearly tables every five,
  instead of every ten seconds. Nothing is fetched while the page is in a background tab.
- The page and its data are sent compressed.

## 0.16.2

- The power flows chart is now the Power Metrics Chart, in the colours of Sigenergy's app.

## 0.16.1

- Power flows chart: the battery is drawn with discharging above zero and charging below,
  and each line has a faded fill down to zero.

## 0.16.0

- Live tiles: "Right now" and "Where it's going" follow Home Assistant as the sensors
  change, instead of waiting for the next stored reading. Nothing extra is stored.
- Tariff rates: any number of time windows (up to six) with their own prices, not just one
  cheap period.
- Half-hourly tariffs: a tariff can follow Octopus Agile's published prices, for your own
  rates and for tariffs you compare against.
- Compare tariffs: a "With charging moved" column shows each tariff with battery and car
  charging moved to its cheapest times.
- Running costs by device: what the heat pump, the EV charger and the rest of the house
  cost, by month and year.
- Bill check: rates read from a bill PDF are compared with the tracker's, and a
  correction is offered where they differ.
- Payback: a switch for whether extra income is assumed to carry on in the projection.

## 0.15.0

- Folded sections and folded bill years are now saved with the app instead of in the
  browser, so they stay folded on every device and after the browser's data is cleared.

## 0.14.0

- Charts: click a legend entry to show or hide that series, on every chart.
- Power charts: choose the last 24 hours, today, the last 7 days or the last 30 days.
- Bill check: bills are grouped under their year, and each year folds away.

## 0.13.1

- Bill check: bills are listed in date order (click the heading to reverse it), with totals
  underneath and a one-line summary of how the bills and the tracker differ overall.

## 0.13.0

- A Sections menu at the top right, always in view, to jump to any part of the page.
- Every section can be folded away by clicking its heading; what is folded is remembered.
- Form labels containing a currency symbol no longer break across several lines.

## 0.12.1

- Bill check: choose several bill PDFs at once, review what was read, and save them together.

## 0.12.0

- Bill check can read the figures from a bill PDF (E.ON Next and Octopus Energy layouts), on
  your own device, for you to check and save.

## 0.11.0

- Monthly summary: a month on one page, compared with the month before and a year earlier,
  with a print button.
- Bill check: enter a bill's figures to compare them with what the tracker measured.
- Currency: new `currency_symbol` and `currency_minor` options.
- CSV money columns are now headed `import_cost` and `export_credit`.

## 0.10.0

- Performance: self-sufficiency, solar used at home, battery efficiency and dear-rate import,
  overall and month by month.
- Would a bigger battery pay? Replays your days to estimate what extra capacity would save.

## 0.9.2

- Tidier settings forms: fields line up in every row, and the payback settings are grouped
  into cost and yearly assumptions.

## 0.9.1

- Extra income: works with the HACS "Axle VPP" integration's separate sensors, and says
  when the Axle sensors are present but unavailable.

## 0.9.0

- Extra income: record payments on top of your tariff, by hand or automatically from an
  Axle Energy event sensor. The total counts towards payback.

## 0.8.0

- History: energy and cost for any past day, week, month or year, as a chart and a table.
- Download any period as a CSV file, by half hour, hour, day or month.

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
