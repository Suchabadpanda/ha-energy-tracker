# Changelog

## 0.41.0

- **Averages opens in a moment.** Each finished day's figures (energy, cost, devices, the
  weather) are now kept in the database once worked out, so opening Averages only works
  out the days since it was last opened, instead of the whole history every time. On a
  year of readings it went from about 1 second to 0.04 seconds here, and should go from up
  to 20 seconds to well under a second on a Home Assistant Green.
- **Running costs by device** keeps finished days the same way (0.7 s to under 0.02 s), and
  **Costs by year** keeps finished months (about 0.95 s to 0.3 s).
- Kept figures are worked out again by themselves when something changes them: readings
  arriving late for an earlier time (history brought in after a break, Import older
  history), published prices for half hours already past, new or changed tariff rates, the
  EV and heat pump options, and each update of the app. A day is only kept once it has
  been over for an hour.

### How to check this release

1. After updating, open History → Averages once. The first time it works out every day,
   which can take as long as before (up to about 20 seconds on a Green), and keeps them.
2. Open another page, come back to History and open Averages again (or restart the add-on
   first): it should now appear almost at once, with the same figures.
3. Devices → Running costs by device and History → Costs by year: the same figures as
   before, and quicker on the second opening.
4. Add a test tariff under Settings → Tariff rates with a start date in the past, check
   that Averages' cost per day changes to match, then remove it and check the figures
   return.

## 0.40.0

- **Gas removed**: the Gas section, its column under Costs by year and its backup entries
  are gone, and any gas bills entered in 0.39.0 are deleted. Extra income under Costs by
  year stays.
- **Quicker**, measured on a year of readings at full detail:
  - Averages works out in about a third of the time, and in a fraction of a second when
    Running costs by device has already been worked out, as both now share one set of
    day-by-day device figures instead of each working them out.
  - The last reading of each energy counter in every half hour is now kept in a small
    summary table, so figures over months and years no longer read every stored reading
    each time. Opening the History page from cold takes about 35% less time; the month's
    cost, asked for every 10 seconds on the Now page, about 85% less.
  - Heat pump and the weather only needs each day's kWh, and now works just that out.
  - The half-hour loops behind every cost figure do less work per half hour.
  - The page's script is compressed once (to about a quarter of its size) rather than for
    every visit.
- **Less work in the background**: figures over months and years (Costs by year, the
  reports, Averages, Performance, Return on investment, Compare tariffs, the planner) are
  worked out afresh every hour instead of every 10 minutes, unless something they depend on
  changes. The pages in use are remembered across a restart, so their figures are ready
  again straight after one.

### How to check this release

1. After updating, the add-on's log shows no errors. The first time a page with long-range
   figures opens it may take a few seconds, while the half-hour summary is filled in; after
   that it is quicker than before.
2. Money page: there is no Gas section. History → Costs by year: no Gas column; with extra
   income, the Extra income and Net after income columns are still there.
3. History → Averages: opens noticeably quicker than in 0.39.0, with the same figures as
   before (compare a month you noted down, if you have one).
4. Devices → Running costs by device, and the costs on the Now page, show the same figures
   as before for past months.
5. Restart the add-on, wait a minute, then open the History page: its sections should be
   ready straight away, as the page was in use before the restart.

## 0.39.0

- **Costs by year** shows **Extra income** (as a minus) and **Net after income** when there
  is extra income, such as Axle payments. A tick box above the month-by-month grid shows the
  total there instead of the electricity's net cost.
- **Gas**, on the Money page: enter gas bills by hand (dates, cost, and optionally kWh). The
  cost is spread over each bill's days and added to Costs by year as a **Gas** column, with
  the last column becoming **Total, with gas**. Optional: **This home has no gas** hides it
  and leaves gas out of the totals. Gas bills are included in the settings backup; restoring
  an older backup leaves them as they are.

### How to check this release

1. On History → Costs by year: with extra income entered, the Extra income and Net after
   income columns appear, and Net after income is the net cost less the income.
2. Under Net cost month by month, tick **Take off extra income**: the months change to the
   totals and the heading changes. The tick is remembered after reloading.
3. On Money → Gas, add a bill (say 1 to 31 January, £100, 1,000 kWh). It shows 31 days,
   £3.23 a day and 10.00p per kWh. Adding a second bill whose dates overlap it is refused.
4. Back on History → Costs by year: a Gas column appears with the January share, and the
   last column is Total, with gas. A year not fully covered by gas bills is marked *.
5. On Money → Gas, choose **This home has no gas: hide this**: the section shrinks to one
   line and the Gas column leaves Costs by year. **This home has gas** brings both back.
6. Settings → Backup and restore → Download settings: the file lists the gas bills when
   checked with **Check file**.

## 0.38.0

- **The app is now five pages** instead of one long one: **Now**, **History**, **Devices**,
  **Money** and **Settings**, linked at the top of every page. The Sections menu lists the
  pages, with the current page's sections under it.
  - Each page only loads and refreshes its own sections. The live tiles (every couple of
    seconds) only run on the Now page.
  - The look and the code are shared files your browser keeps between pages, and fetches
    again only after an update.
  - All pages share the same readings and settings.
- The figures the app works out in the background are only kept ready for pages opened in
  the last day, so the Home Assistant Green does far less work when nobody is looking. A
  page not opened for a while works its figures out when opened (a few seconds), and is
  kept ready again from then on.
- Choosing a month under Costs by year opens that month's costs on the Now page.

### How to check this release

1. Update the add-on and open Energy Tracker. The Now page should show the tiles, today's
   and the month's costs and the power charts, with the five page names in the header.
2. Open each of History, Devices, Money and Settings: every section from before should be
   on one of them (the table in the user guide lists which), with no "Could not…" messages.
3. Open **Sections** on any page: it lists all five pages and that page's sections.
4. On History, under Costs by year → Net cost month by month, choose a month: the Now page
   opens at that month's costs.
5. Fold a section on one page, then open another page and come back: it is still folded.
6. Optional, for the speed: in the add-on's **Info** tab, CPU use should drop when the page
   is closed, compared with before.

## 0.37.0

- **Heat pump COP today** tile under Cost today, and **Heat pump SCOP** for the month under
  the month's cost, each with heating and hot water apart. Estimated from the heat pump's
  modes, as in Estimated SCOP.
- **COP yesterday** tile in the Estimated SCOP box.

## 0.36.0

- **Estimated SCOP from the heat pump's modes**, for this month and this year, with a table
  by year and month. Name the entity that shows a hot water run (`hot_water_mode_entity`,
  such as Samsung's `water_heater.heat_pump`) and the one that shows heating is on
  (`heating_mode_entity`, such as `climate.heat_pump_indoor1`, whose current temperature is
  the flow temperature), and enter the cylinder's litres under Your heat pump's details.
  - The electricity is split exactly into hot water, heating and standby by the mode at
    the time.
  - Hot water heat is measured from each reheat's temperature rise; heating heat is
    modelled from the flow and outdoor temperatures, calibrated on those reheats.
  - Days before the modes were collected are estimated from the heat pump's total, so the
    year goes back as far as its readings.
- The heat pump split tile and Today by device use the modes when they are there.
- The heat loss survey is now optional; its figures are labelled Survey SCOP.

## 0.35.0

- Heating and hot water are now **measured** when the hot water cylinder's temperature
  sensor is named in the new `hot_water_tank_entity` option: half hours in which the tank
  heats up count as hot water. Used by the Heat pump split tile and the estimated SCOP. Days
  the tank readings don't cover are estimated as before. The tank's recent history is
  brought in from Home Assistant on start.
- Today by device shows the heat pump's heating and hot water in kWh.
- Temperatures up to 100 °C are accepted, for a hot water tank.

## 0.34.0

- **Estimated SCOP** under Heat pump and the weather: enter the figures from your heat loss
  survey and the tracker estimates the heat your home needed from how cold each day was,
  divided by the electricity the heat pump used for heating, by month and winter. With the
  survey's degree days and design SCOP, it also shows the yearly heat need and the
  electricity that would take.
- The heat pump tiles show their units small, so the figures fit.

## 0.33.1

- Heat pump and the weather could disappear for good if it had been folded, as it stayed
  hidden until opened but could not be opened while hidden. It is now shown until it is
  known that there is no heat pump, and shows any error rather than vanishing.

## 0.33.0

- New **Averages** section: everything per day, by month or by year, so periods of any
  length compare fairly. Used, house load, EV charger, heat pump, solar, import, export,
  self-sufficiency, net cost and outdoor temperature. Tiles compare the last 30 days with
  the 30 before and the same time last year, with weekdays against weekends and the
  busiest and quietest days.

## 0.32.6

- The Battery charge tile shows the kWh left in the battery and a bar of how full it is.

## 0.32.5

- "Where it's going" and "Today by device" now list house load, EV charger, then heat pump,
  the same order as Cost by device.

## 0.32.4

- The note shown when a tariff has no VAT now just says so, without suggesting 5%: VAT on
  electricity can be 0%.

## 0.32.3

- The cost tiles have coloured stripes: net cost, import, export credit, standing charge
  and saved by off-peak.
- When a tariff has no VAT set, the cost sections say so.

## 0.32.2

- Cost by device: each device's kWh is now worked out over the whole day, so the house load
  matches Today by device. Before, half hours where a slowly reporting meter (such as a
  heat pump's through SmartThings) seemed to use more than the house did could add about a
  kWh a day to the house load.

## 0.32.1

- New **Battery export** in cost by device and Running costs by device: what it cost to
  store electricity that was later sold back to the grid from the battery. Before, that
  cost was counted against nothing, so the devices fell well short of the import cost in
  months with export events.
- Cost by device percentages are now of the total, battery export included.
- Running costs by device lists house load, EV charger, heat pump, then battery export, in
  the same order as the tiles.

## 0.32.0

- New option `heat_pump_energy_entity`: the heat pump's own energy meter (such as a
  Samsung heat pump's, through SmartThings). The heat pump's use is then taken from it
  rather than the smart load circuit, everywhere device figures appear, and **Import older
  history** brings in its past readings from Home Assistant.

## 0.31.6

- Device figures now start from when every device counter was being read. Before, months
  before the smart load sensor was switched on showed the heat pump as using nothing and
  counted its use as house load.

## 0.31.5

- Removed the "true cost" figures from the heat pump tiles, to keep cost by device simple.

## 0.31.4

- "Rest of house" is now called "House load" throughout.
- Cost by device runs house load, EV charger, heat pump, then the heat pump split.

## 0.31.3

- Cost by device: the heat pump, EV charger and rest of the house tiles come first, then
  one split tile for the heat pump's heating and hot water.

## 0.31.2

- Heating and Hot water are back under today's and the month's cost, as tiles beside the
  heat pump, each with its energy and true cost.

## 0.31.1

- Removed the heat pump table (heating, hot water and true cost) from under today's and
  the month's cost. The heat pump's tile still shows its true cost.

## 0.31.0

- Device costs now follow the energy through the battery, half hour by half hour: grid
  power at that half hour's price, battery power at what charging the battery cost, solar
  free. A car charged overnight costs the night rate, and the house run from the battery by
  day carries the cost of charging it. Used for cost by device, Running costs by device and
  the heat pump's true cost (which now counts solar stored in the battery too).
- Without battery readings, the old way (each day's import cost shared by use) is used.

## 0.30.0

- The heat pump's cost, under today's and the month's cost, is split into heating and hot
  water, and given a **true cost**: its import cost plus the export payment given up on the
  solar it used.

## 0.29.0

- Cost today and the month's cost show the import cost by device: the heat pump (or other
  smart load), the EV charger and the rest of the house, with the energy each used and its
  share. The three add up to the Import figure.

## 0.28.1

- The **Sections** menu links to the user guide, the overview page and what's new, so
  help is a click away while using the app. They open on GitHub in a new tab.

## 0.28.0

- Extra income is sorted into **regular** (Axle's monthly payments and events) and
  **one-off** (a referral bonus, say). Each entry shows which it is and can be changed with
  Edit. Existing entries are sorted for you: anything from or described as Axle is regular.
- Return on investment carries only regular income forward in its projection, unaffected
  by panel or battery ageing and price changes. One-off income still counts towards what
  has been saved.
- New setting: **Regular income of at least (£ a month)**, such as Axle's £10 minimum, so
  the projection never assumes less.

## 0.27.1

- Faster: each section is fetched only when it is scrolled near, so the top of the page
  appears first. Opening the page from cold asks for about a third as much.
- Faster: figures that are a little old are shown at once and brought up to date in the
  background, instead of making the page wait. The yearly report and the panels estimate
  are now prepared in the background too, and everything is prepared again straight after
  a change rather than on the next visit.
- Faster: the half-hour arithmetic behind the long-range figures does less repeated work.

## 0.27.0

- Heat pump and the weather: each day's heat pump use plotted against the outdoor
  temperature, with how much more it uses for each degree colder, its use on warm days
  (mostly hot water) and kWh per degree day for each month and winter, so winters can be
  compared fairly whatever the weather.
- The outdoor temperature is read from Home Assistant's weather entity, or from a sensor
  named in the new `outdoor_temperature_entity` option. Earlier temperatures Home Assistant
  already holds are brought in once.

## 0.26.0

- Performance: "Would more panels, or a bigger inverter, pay?" estimates the yearly gain
  from 1 to 4 kWp more panels, what the inverter limit would cost, and the payback time.

## 0.25.0

- Return on investment: "Also compare with" draws a second savings line, with its own
  projection and break-even date, against another tariff such as the standard variable rate.
- Tariffs can follow the published prices of any Octopus import tariff, not just Agile. A
  variable tariff such as Flexible Octopus then uses each past price at the time it applied.

## 0.24.0

- Notes: pin a note to a day ("new tariff", "heat pump serviced"). Notes show as markers on
  the history and return on investment charts, and are included in backups.
- Yearly report: "Hide costs" leaves out everything about money, for sharing.

## 0.23.1

- Return on investment: the line from your bills is projected forward too, with its own
  break-even date and profit at the end of the chart.

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
