# Energy Tracker

> **Built for Sigenergy solar and battery systems.** It reads the sensors created by the
> Sigenergy integration for Home Assistant. Other inverters will not work without editing
> the sensor list (see "Using different sensors").

## Finding your way around

The page is long, so the header stays in view as you scroll. **Sections**, at the top
right, lists every part of the page: choose one to go straight to it. Click any section
heading to fold that section away, and again to bring it back; **Fold all sections** and
**Open all sections** are at the foot of the menu. What you fold is saved with the app, so
it stays folded the next time you open it, on any device or browser.

Folding sections you rarely look at also makes the page quicker to open: nothing is
fetched for a folded section until you open it.

## How often things update

| Part of the page | Updates |
|---|---|
| Right now, Where it's going | Within a couple of seconds (live) |
| Today's energy and costs, this month's cost | Every 10 seconds |
| Power charts | Every minute |
| History, extra income, rates, costs by year | Every 5 minutes |
| Device costs | Every 10 minutes |
| Payback, performance, comparison | Every 30 minutes |

The long-range figures take a while to work out on a small machine, so the app prepares
them in the background every ten minutes and keeps them ready. They can therefore be up to
about twenty minutes behind; saving rates, a bill, a tariff or income works them out again
at once. Nothing is fetched while the page is in a background tab.

## Charts

- **Choosing what a chart shows**: each entry in a chart's legend is a switch. Click
  "Imported" to hide it, and again to bring it back; hide all but one to study a single
  series. The chart rescales to what is left. At least one series always stays on, and the
  choice is remembered on that device. This works on every chart.
- **Exact values**: point at the Power Metrics Chart (or touch it) to mark a moment and
  list each line's value then, with whether the grid was importing or exporting and the
  battery charging or discharging. The history chart does the same for each bar.
- **Power charts**: the selector beside **Power Metrics Chart** sets the period for both power
  charts: the last 24 hours, today (midnight to midnight), the last 7 days or the last 30
  days. Longer periods are averaged into wider steps, so short spikes are smoothed out, and
  they refresh every five minutes instead of every ten seconds. They can only go back as
  far as readings have been collected.

## First steps

1. Start the app and open **Energy Tracker** from the sidebar.
2. Under **Tariff rates**, choose **Add or change rates** and enter your own prices. The
   rates it starts with are only an example.
3. Optional: under **Older history**, import earlier readings so past months have costs.

## Options

| Option | Default | Meaning |
|---|---|---|
| `poll_seconds` | 30 | How often sensors are read (10 to 3600). Lower uses more storage. |
| `backfill_days` | 10 | How far back to fill gaps from Home Assistant's history. 0 turns it off. |
| `detail_days` | 30 | How long full-detail readings are kept before being thinned to one per 5 minutes. |
| `keep_years` | 10 | Readings older than this are deleted, a day's worth each day, so storage stops growing. Costs for months older than this are no longer shown. |
| `currency_symbol` | £ | The symbol shown before amounts of money. |
| `currency_minor` | p | The small unit prices are entered in, such as p or c. One hundred of them make one of the main unit. |
| `axle_event_entity` | sensor.axle_event | Only for the manual Axle set-up with a differently named sensor. The HACS "Axle VPP" integration is found without it. |
| `smart_load_label` | Heat pump | What is connected to the gateway's smart load port 1, as named on the dashboard. |
| `ev_on_smart_load` | true | On if the EV charger is wired through smart load port 1. Its use is then taken off the smart load figure. Turn off if the charger is on its own circuit. |

## Optional equipment

The EV charger and smart load tiles appear only if their sensors exist in Home Assistant.
With neither, the "Today by device" section and the breakdown chart are left out.

- **Smart load**: `sensor.sigen_plant_smart_load_1_power` and
  `sensor.sigen_plant_smart_load_1_total_consumption`.
- **EV charger**: `sensor.tesla_wall_connector_total_power` and
  `sensor.tesla_wall_connector_energy` (Tesla Wall Connector integration). For another
  charger, change these two entities as described below.

## Using different sensors

1. Copy [`config/metrics.toml`](https://github.com/Suchabadpanda/ha-energy-tracker/blob/main/energy_tracker/config/metrics.toml)
   into this app's config folder (the `addon_configs` share, in the folder ending
   `_energy_tracker`), keeping the name `metrics.toml`.
2. Change the `entity` values to your sensors. Keep the `name` values as they are.
3. Restart the app.

Power sensors may be in W or kW and energy sensors in Wh, kWh or MWh; they are converted.
Grid power must be positive when importing, and battery power positive when charging.

## Live tiles

**Right now** and **Where it's going** follow Home Assistant directly: when a power sensor
or the battery level changes, the tile shows it within a couple of seconds. The status at
the top of the page reads **Live** while this is working. How quickly a figure moves is
then set by how often the Sigenergy integration itself updates its sensors.

These live values are only shown, not stored. Everything else on the page (charts, energy
totals, costs) still comes from the readings stored every `poll_seconds`. If the live
connection drops, for example while Home Assistant restarts, the tiles carry on from the
stored readings and reconnect on their own.

## Costs

- Costs come from the inverter's lifetime import and export counters, split into half-hour
  slots and priced with the rates that applied on each day. Expect small differences from
  your bill.
- Enter prices before VAT and set the VAT percentage, or enter prices including VAT and
  set VAT to 0. VAT is added to import and the standing charge, not to export.
- "Saved by off-peak" is what the same import would have cost at the dearest rate, minus
  what it did cost. On half-hourly prices it is measured against each day's average price.

### Time windows

Under **Tariff rates**, give a **standard rate**, then **Add a time window** for each
period of the day with a different price: one for a single cheap period, or up to six for
tariffs with several. A window may run past midnight (23:30 until 05:30). With no windows,
the standard rate applies all day.

### Half-hourly prices (Octopus Agile)

For a tariff whose price changes every half hour, choose your region and the tariff under
**Half-hourly prices**. Each half hour is then charged at the price Octopus Energy
published for it.

- Published prices already include VAT, so VAT is not added to them. The standing charge
  is still what you type in.
- Prices are fetched within a minute of saving, back to when your readings began, and
  topped up every hour (tomorrow's are published in the afternoon). The **Tariff rates**
  table shows the dates they are held for, and says if a fetch failed.
- The rates you type in are still needed: they are used for any half hour with no
  published price, such as dates before the tariff existed.
- Only a tariff code and a region letter are sent to Octopus Energy.
- Performance treats the cheapest four hours of each day as that day's cheap rate.

## Running costs by device

Shown when a smart load or an EV charger is fitted. For today, and for each month and year,
it gives the energy each device used and its share of the import cost. Click a year to show
or hide its months.

Each day's import cost is shared out by how much of that day's electricity each device
used: a heat pump that used 40% of the day's consumption carries 40% of what was paid for
import that day. This spreads cheap overnight battery charging across whatever the battery
later powered, without following each unit through the battery. As a result:

- A device run mostly from solar still carries a share of the day's import, and a car
  charged overnight is costed at the day's average price, not purely the night rate.
- The standing charge and export income are left out. They belong to the house as a whole.

## History and downloads

**History** shows any past day, week, month or year: solar generated, energy used, imported
and exported, as totals and as a bar chart split into hours, days or months. Use the arrows
to step back and forward, and **Latest** to return to the current period. Point at a column
(or tab to it) to see its figures, or open **Show as a table**.

It is worked out from the energy counters, so it covers the whole of the stored history,
including anything brought in by **Import older history**.

**Download CSV** saves the period being viewed as a spreadsheet file, with one row per half
hour, hour, day or month. Columns are energy in kWh for every counter collected, plus import
cost and export credit in your currency at the rates in force at the time. The standing charge is
not included in the rows. Times are local, and an empty cell means there was no reading.
Half-hourly rows are only as detailed as the stored readings: imported history is hourly, so
its half hours are an even split.

If the download does not start inside the Home Assistant phone app, open Home Assistant in
a web browser instead.

## Performance

**Performance** measures how the system is doing over the last 30 days, 90 days, 12 months
or everything stored, in whole days up to yesterday.

- **Self-sufficiency**: the share of what you used that did not come from the grid.
  Charging the battery from the grid counts as grid use, so a household that runs mostly
  on cheap-rate battery power will show a low figure here and a low dear-rate import.
- **Solar used at home**: the share of what the panels made that was not exported.
- **Battery efficiency**: energy out for every 100 in. Shown once there are two weeks of
  battery readings, because over a short time the battery may just be fuller or emptier
  than it started.
- **Dear-rate import**: everything bought outside your cheapest rate, with its cost.

**Month by month** gives the same figures for each month.

### Would a bigger battery pay?

On a tariff with a cheap rate, this replays the days measured and asks what would have
happened with more capacity. Energy bought at a dearer rate on a day is what the battery
failed to cover. Extra capacity, charged at the cheap rate, would have avoided up to its
own size of that, once a day. The saving is the dearer price avoided, less the cheap price
of charging, allowing for the battery's measured efficiency (90% until that is known).

It is an upper limit. It assumes the inverter can supply the extra power when it is wanted,
that the cheap window is long enough to fill the extra capacity, and that your usage stays
the same. With under a year of readings the yearly figures are scaled up and marked rough.
To judge a purchase, divide the price of the extra battery by the yearly saving.

## Monthly summary

**Monthly summary** puts a calendar month on one page: what it cost and why, how that
compares with the month before and the same month last year, what the panels made, how
much was bought outside the cheapest rate, any extra income, and the dearest, cheapest,
sunniest and busiest days. It opens on last month; use the arrows for others.

**Print** prints the summary on its own, or saves it as a PDF if you choose that as the
printer. It counts whole days up to yesterday, so the current month is a part month and is
marked as such, as is any month the readings only partly cover.

## Bill check

**Bill check** compares a bill or statement with what the tracker measured over the same
dates. Open **Add a bill** and enter the first and last day billed, and whichever figures
you have: energy imported, the amount charged (energy and standing charge together, with
VAT, before any export payment), energy exported and the export payment.

### Reading a bill PDF

Instead of typing, choose the bill's PDF under **Add a bill** and press **Read bill**. The
form is filled in with the billing dates, units and amounts found, and a note lists the
rates, standing charge and VAT quoted on the bill. Nothing is saved until you press **Save
bill**, so check the figures against the bill first.

### Correcting your rates from a bill

The rates on each bill read are compared with the rates the tracker holds for the bill's
first day. Where they differ, a box lists each difference (cheap rate, day rate, standing
charge, VAT, or export rate) with two buttons:

- **Correct the rates in use**: replaces the wrong figures in the set of rates that covers
  the bill. Use this when the rates were typed in wrongly.
- **Start new rates on** the bill's first day: adds a new set of rates from that date and
  leaves earlier days alone. Use this when the price really changed.

Nothing changes until you press one, and every cost on the page is then worked out again.
Rates are only matched up when the bill and the tracker show the same number of import
rates; otherwise the box says so and the rates are left for you to edit. A bill says
nothing about time windows, so those are never changed.

- The PDF is read on your own device. It is not stored and not sent anywhere.
- It works on PDFs downloaded from the supplier, which contain real text. A scan or a
  photo of a paper bill cannot be read.
- It is built around the bill layout that E.ON Next and Octopus Energy share. Bills from
  other suppliers may give some figures or none; type in whatever is missing.
- Choose several PDFs at once to read them into a list. Each is shown with what was found;
  any that could not be read, or that match a bill already saved, are skipped. **Save**
  then stores the rest together.
- Import and export statements are read separately; add each as its own entry.
- On a dual-fuel bill only the electricity is read.

Bills are grouped under the year most of their days fall in, with a line for each year
giving its count and totals; click a year to fold its bills away. Within a year they are in
date order by the first day billed; click **Bill period** to switch between oldest first
and newest first. Underneath, **Total** adds up the bills, the
tracker's figures for the same bills, and the difference, and a line at the top says the
same in words. Totals compare like with like: each column adds up only the bills that have
that figure, and a bill is left out if the tracker's readings do not cover all of its dates.

Each bill is shown above the tracker's own figures and the difference between them. The
inverter and the supplier's meter are separate instruments, so a percent or two is normal.
A larger gap in kWh points at the metering; a gap in money with matching kWh points at the
rates entered under **Tariff rates**, or at an estimated reading on the bill.

## Other currencies

Set `currency_symbol` and `currency_minor` on the Configuration tab to show amounts in
another currency. Prices are always entered in the small unit per kWh, with one hundred to
the main unit. The Octopus Energy price lookup is for Great Britain only.

## Payback

**Payback** shows how much the system has saved, how much of its cost that covers, and an
estimated break-even date, with a chart of savings building up towards the cost.

Open **System cost and settings** and enter the install date and what you paid. Later
additions (more panels, a second battery) can be added as further lines with their dates.

How it is worked out:

- **Saving each day** = what your household's whole consumption would have cost bought from
  the grid, with no export income, less what you actually paid.
- **"Without the system I would be on"** sets the prices used for that. The default is your
  own tariff, so anything you would run at the cheap rate anyway, such as charging a car
  overnight, still counts as cheap. To measure against another tariff, add it under
  **Compare tariffs** and choose it here.
- **Break-even estimate**: with a full year of readings, the last 12 months are repeated
  into the future, so the seasons are respected. With less than a year the average is
  carried forward and the estimate is marked **rough**; it firms up as readings build.
- Time between the install date and the first reading is filled in at the average, and the
  amount is stated under the chart.
- **Extra income** always counts towards what has been saved. Under **System cost and
  settings**, untick **Assume extra income carries on** to leave it out of the yearly
  figure and the projection: sensible if the payments are occasional or may stop.

### Optional yearly assumptions

The main estimate assumes today's prices and performance continue. Fill in any of these to
see a second break-even date, and a second line on the chart, that allows for them:

| Setting | Typical | What it does |
|---|---|---|
| Panels lose each year | 0.5% | Shrinks the solar part of the savings. Panel warranties usually promise 85 to 90% of output after 25 years. |
| Battery loses each year | 2% | Shrinks the battery part of the savings. Battery warranties are often 70% of capacity after 10 years; check yours. |
| Electricity prices change each year | none | Scales all savings up or down. There is no reliable forecast, so enter your own view. A rise brings break-even forward, so it is the assumption most likely to flatter the result. |

To apply the ageing rates to the right part, savings are split in two. The solar part is
what the panels alone would have saved with no battery: generation used directly in each
half hour, at the import price for that time, plus the rest exported. The battery part is
everything the system saved beyond that. Both yearly figures are shown under the chart.

Neither estimate allows for maintenance, replacement parts, import and export prices
moving differently from each other, or what the money might have earned elsewhere.

## Extra income

**Extra income** is for money earned on top of your tariff, such as Axle Energy payments
for exporting during grid events. The total counts towards **Payback**. It is kept separate
from the cost figures, which stay as what your supplier charges.

- **By hand**: open **Add income or change settings**, and enter the date and amount.
- **Automatically, for Axle Energy**: if Home Assistant has Axle's sensors, each export
  event is recorded when it appears. Both set-ups in Axle's Home Assistant guide work: the
  "Axle VPP" integration from HACS (`sensor.axle_start_time`, `sensor.axle_end_time` and
  `sensor.axle_import_export`) and the single `sensor.axle_event` made by the manual set-up.
  The section says whether the sensors were found and whether they are reporting. A few minutes after it ends, the energy exported
  between its start and end is multiplied by the rate you set (100p per kWh to begin with)
  and saved as an **Estimate**.

Axle publishes when events run, but not what they paid. The estimate is every unit
exported during the event at your rate, which may differ from Axle's own calculation. When
you know the real payment, choose **Edit** on the entry and enter it; the estimate label
is then dropped. Changing the rate affects events measured afterwards, not earlier ones.

Only export events are recorded. An event shown while the app was stopped is picked up
from Home Assistant's history when it restarts, for as far back as `backfill_days`.

## Comparing tariffs

**Compare tariffs** shows what your real imports and exports would have cost on other
tariffs, over the last 30 days, 90 days, 12 months or everything stored.

- **By hand**: open **Add a tariff to compare** and enter the prices. Give a standard rate,
  then use **Add a time window** for each period with a different price (a window may run
  past midnight). Up to six windows.
- **Price change on your own tariff**: choose **Copy my current rates**, alter the prices,
  and save. This shows what an announced change would cost over a year of your usage.
- **Look up Octopus Energy prices**: choose your region and a tariff, then **Fill in
  prices**. The form is filled with today's published prices including VAT, with VAT to
  add set to 0. Check the export rate (it is left as your own), then save.
- **Half-hourly prices (Octopus Agile)**: choose your region and the tariff under "Or
  follow half-hourly prices". Each half hour is priced at what was published for it; the
  rates typed in cover any half hour without a published price.

Two costs are shown for each tariff:

- **Net cost** replays your usage exactly as it happened. A tariff whose cheap window
  differs from yours looks dearer here than it would be in practice, because your battery
  and car were charging to suit your current window.
- **With charging moved** also moves the import that went into the battery and the car to
  the tariff's cheapest half hours of the same day, no faster than they have actually
  charged. Everything else stays where it was. It is a best case: it does not check that
  the battery would last until its next charge, so a tariff whose cheap hours fall late in
  the day may do a little worse. It needs the battery charge counter (and the EV charger's,
  if there is one); without them the column shows a dash.

Things to know:

- Looked-up prices are today's. They are not updated afterwards, and earlier months are
  priced at today's rates.
- The lookup covers Octopus Energy only, and "Fill in prices" leaves out tariffs that
  track the wholesale price. Other suppliers do not publish a price list that an app can
  read.
- The price list is only contacted when you open the form or press **Fill in prices**. A
  region letter and tariff code are sent; nothing else.

## Older history

**Import older history** fetches hourly history for the energy counters from Home
Assistant's long-term statistics. For the time before Home Assistant has any, add an
export from the mySigen app (.xlsx, hourly or daily). Hourly is much better: with a daily
export the time of day is unknown, so 99% of each day's import is assumed to fall in the
cheap window.

### Getting an hourly export from Sigen AI

The mySigen app's assistant, Sigen AI, can email you the file.

1. Open the mySigen app (or the mySigen web dashboard) and tap the floating Sigen AI icon,
   a small round robot face, to open the chat.
2. Ask for the data, giving the day your system was installed and today's date:

   > Can you export hourly energy data in kWh for grid import, grid export, load, solar
   > generation, battery charge and battery discharge between 29 October 2025 and
   > 9 April 2026?

3. Sigen AI replies with a summary of what it will export. Check that **Granularity** says
   **Hourly**. It may offer daily first; if so, reply "can I have the data in hourly
   granularity".
4. Confirm when it asks whether to proceed. The export only reads data; it changes nothing
   on your system.
5. The file is emailed to your mySigen account's address. A long period takes a few minutes
   to arrive; check the spam folder if it does not. If it arrives as a .zip, unzip it to get
   the .xlsx file.
6. In Energy Tracker, open **Older history > Import older history**, choose the .xlsx file,
   click **Check only** to preview, then **Import**.

It is fine for the export to run past the date Home Assistant's own history starts: the
import uses the file only for the time before that. Importing again with a better file
replaces what the earlier one brought in.

## Security and data

- The dashboard opens through Home Assistant, behind its login. No network port is opened.
- The only outside service contacted is Octopus Energy's public price list, and only when
  you use the tariff lookup.
- The app only reads from Home Assistant. It never changes a device or setting.
- Readings and rates are stored in the app's data folder and are included in Home
  Assistant backups. The app pauses for a few seconds while a backup is taken.

## Storage

The database cannot grow without limit:

- The last `detail_days` (30) are kept in full detail.
- Older readings are thinned to one every 5 minutes.
- Readings older than `keep_years` (10) are deleted.

With the default sensors that levels off at roughly 0.9 GB when polling every 30 seconds,
or 1 GB at every 10 seconds, reached after ten years. The current size is shown at the
bottom of the dashboard. Home Assistant backups include the database.

## Support

This is a personal project shared as it is, with no guarantee of support.

It is free to use. If you would like to say thanks, you can
[buy me a coffee](https://buymeacoffee.com/badpanda).
