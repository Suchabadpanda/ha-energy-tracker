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

The top of the **Sections** menu also links to this user guide, the app's overview page
and the list of changes in each version. They open in a new tab, on GitHub, so they need
an internet connection.

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
| Return on investment, performance, comparison | Every 30 minutes |

The long-range figures take a while to work out on a small machine, so the app prepares
them in the background every ten minutes and keeps them ready, so the page never waits for
them. They can therefore be a few minutes behind; saving rates, a bill, a tariff, a note or
income works them out again straight away, in the background.

Each section is only fetched once it is scrolled near, and not at all while it is folded,
so the top of the page appears quickly. Nothing is fetched while the page is in a
background tab.

## Colours

Energy colours follow Sigenergy's own app and mean the same everywhere on the page: yellow
for solar, purple for consumption (and the house load), lilac-blue for the grid and
import, cyan for the battery, green for export, orange for the heat pump and blue for the
EV charger. Each tile has a stripe in the colour of what it measures. Green and red on
their own mean cheaper or better, and dearer or a problem.

## Charts

- **Choosing what a chart shows**: each entry in a chart's legend is a switch. Click
  "Imported" to hide it, and again to bring it back; hide all but one to study a single
  series. The chart rescales to what is left. At least one series always stays on, and the
  choice is remembered on that device. This works on every chart.
- **Exact values**: point at either power chart (or touch it) to mark a moment and
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
| `outdoor_temperature_entity` | (empty) | A temperature sensor outside, for the heat pump and the weather. Left empty, the first weather entity is used (Home Assistant sets one up for your home). |
| `heat_pump_energy_entity` | (empty) | The heat pump's own lifetime energy meter, if it has one in Home Assistant (a Samsung heat pump through SmartThings, say). When set, the heat pump's use comes from it instead of the smart load circuit, and **Import older history** can bring in its past readings. |
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
- **Cost by device**, under Cost today and the month's cost, gives what the house load
  (everything except the heat pump and EV charger), the EV charger and the heat pump (or
  other smart load) cost, in that order. It is shown when a smart
  load or an EV charger is fitted, and worked out the same way as **Running costs by
  device** below, following the energy through the battery. A line underneath says how much
  of the period's import is still in the battery, or how much was bought before it. The
  standing charge and export credit are left out, as they belong to the house as a whole.
- After the devices, a **split** tile divides the heat pump's cost into **Heating** and
  **Hot water**. Hot water is
  taken as up to the heat pump's usual use on warm days (worked out under **Heat pump and
  the weather**); anything above that on a day is heating. Hot water takes more energy in
  winter, so in cold months it is likely to be a little more than shown. The tile appears
  once there are a few warm days to go on.

### Time windows

Under **Tariff rates**, give a **standard rate**, then **Add a time window** for each
period of the day with a different price: one for a single cheap period, or up to six for
tariffs with several. A window may run past midnight (23:30 until 05:30). With no windows,
the standard rate applies all day.

### Following Octopus's published prices

For an Octopus tariff whose price changes every half hour (Agile) or from time to time (a
variable tariff such as Flexible Octopus), choose your region and the tariff under
**Published prices to follow**. Each half hour is then charged at the price Octopus Energy
published for it, so past price changes are included.

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
it gives the energy each device used and what it cost. Click a year to show or hide its
months.

The energy is followed to where it came from. In every half hour the house is supplied by
solar, the grid and the battery, and each device pays its share of that half hour's
supply:

- **Grid power** at that half hour's price, with VAT.
- **Battery power** at what putting the energy into the battery cost. The app keeps a
  running account of the battery: grid charging adds its cost, solar charging adds
  nothing, and losses charging and discharging it are allowed for.
- **Solar** used straight away costs nothing.

So a car charged overnight costs the night rate, and the house run from the battery during
the day carries the cost of charging it overnight. Within each half hour, devices share by
how much each used.

- **Battery export**: electricity stored in the battery and later sold back to the grid,
  during an export event say, is no device's. What it cost to store is shown on its own,
  as Battery export; the payment for it is in Export credit.
- The devices and battery export add up to the import cost, give or take energy still in
  the battery at the end of the period, or bought before it and used from the battery.
- Export: solar is exported before anything is stored, and the battery is charged from
  solar before the grid.
- The standing charge and export income are left out. They belong to the house as a whole.
- Without battery charge and discharge readings, each day's import cost is shared out by
  how much of that day's electricity each device used instead.
- If the heat pump has its own energy meter in Home Assistant, name it in the
  `heat_pump_energy_entity` option. The heat pump's use is then taken from that meter, the
  EV charger from its own counter, and the house load is what is left. The smart load
  circuit is not needed. After setting it, restart the app and run **Import older
  history** to bring in the meter's past readings.
- Device figures start from the first whole day every device counter was being read. If
  the smart load sensor was switched on later than the others, earlier days would
  otherwise show the heat pump as using nothing and count its use as the house's. History
  from before then can only come from Home Assistant's long-term statistics (see **Older
  history**).

## Heat pump and the weather

Shown when a smart load is fitted. Each dot on the chart is a day: the heat pump's use
against that day's average outdoor temperature. The line is the trend on days cold enough
to need heating.

- **Extra use per °C colder**: how much more the heat pump uses on a day one degree colder.
- **Use on warm days**: its use on days above 15.5 °C, which is mostly hot water.
- **kWh per degree day**: each day adds "degree days" for how far its average fell below
  15.5 °C, the usual UK base for heating. Dividing the heat pump's use by them gives a
  figure that can be compared from one winter, or month, to the next whatever the weather.
  If it rises, the heat pump or its settings are working less efficiently. Winters run
  October to April, and months with little heating are left blank, as hot water would
  swamp the figure.

The outdoor temperature comes from Home Assistant's weather entity (a forecast service,
which reports the temperature near you), or from your own outdoor sensor if you name it in
the `outdoor_temperature_entity` option. A sensor outside, out of the sun, is more accurate.
When the app starts it brings in the temperatures Home Assistant already has, once: years
of hourly averages for a sensor with long-term statistics, or about ten days for a weather
entity. A day counts once it has at least 12 hours of readings.

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

### Would more panels, or a bigger inverter, pay?

Below the battery estimate, a table shows what 1 to 4 kWp more panels would add each year:
the extra generation, how much would be lost to the inverter's limit, and the saving.
Enter your system under **Your solar system**: the panels' size, the inverter's limit for
solar, optionally a bigger inverter to compare, and optionally a price per kWp to see how
long each option takes to pay back.

The household is run again on your own tariff with the panels' output scaled up, half
hour by half hour, and the battery working as in the tariff switch planner. Extra solar
covers the house first, then fills the battery, then is exported. Panels facing a
different way would produce at different times of day, so treat the figures as a guide.
The panel size is estimated from the readings until you enter it, and tends to come out
a little low.

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

## Yearly report

A calendar year on one page: what electricity cost, what the panels made, how much of what
you used came from them, what the smart load and the EV charger used, extra income, how
much of the system's cost had been saved by the end of the year, and the year's standout
days. Below that, the year beside the one before, and a line for each month.

Use the arrows to move between years and **Print** to print the report on its own. **Hide
costs** leaves out every figure about money, so the report can be shared or printed for
someone else; press it again to bring them back. The
current year is shown "so far". A change between two years is only worked out when both
are complete, since part of a year against a whole one is not a fair comparison.

## Notes

Under **Notes**, pin a note to a day: a tariff change, a service visit, a new appliance,
time away. Notes show as a small yellow marker on the history chart (in the column for that
day, week or month) and on the return on investment chart; point at a marker to read it.
They are included in **Download settings**.

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

## Return on investment

**Return on investment** (called Payback before 0.18) shows how much the system has saved, how much of its cost that covers, and an
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
- **Also compare with**: under **System cost and settings**, choose a second tariff from
  your comparison list to draw a purple line of savings measured against it instead, with
  its own projection and a tile giving the break-even date. Typically this is the standard
  variable rate: "what has the system saved against doing nothing at all", beside "what
  has it saved me as I actually live". So that past price changes count, set that tariff to
  follow Octopus's published prices (see Comparing tariffs); otherwise its typed-in prices
  are used for every day.
- **Saved, from your bills**: a second line on the chart, and a tile, working out the
  savings from the bills entered under **Bill check** instead of the tracker's own
  costing. On each day a bill covers, what you paid is the bill's charge spread evenly over
  its days, less the payment on an export bill for that day (or, without one, the export
  credit the tracker measured). Days no bill covers use the tracker's figure, and the line
  stops at the last billed day. The note under the chart gives both totals to that day.
  A dashed blue line carries on from the last bill at the rate the bills show, worked out
  the same way as the main projection, with its own break-even date in a tile.
- **Progress bar**: how much of the cost has been paid back, the amount remaining and when
  it is expected to be recovered. Once the system has paid for itself it shows the profit
  so far.
- **Projected profit**: the chart runs on past break-even to a number of years after the
  install date (20 unless you change **Look ahead** under **System cost and settings**).
  Everything above the system cost line is shaded as profit, and a tile gives the figure
  at the end. This assumes the system keeps working at no further cost. A battery or an
  inverter may need replacing within that time, and that is not allowed for, so treat the
  later years as a ceiling, not a forecast.
- **Extra income** always counts towards what has been saved. Only **regular** income
  (such as Axle's monthly payments) is carried forward in the projection, at what the last
  year paid; **one-off** income (a referral bonus, say) never is. Regular income is carried
  forward as it is: it does not age with the equipment or follow energy prices.
  - **Regular income of at least (£ a month)**: Axle pays a minimum each month, so set it
    here (10, say) and the projection never assumes less, even before a year of payments
    has been recorded.
  - Untick **Assume regular income carries on** to leave all extra income out of the
    yearly figure and the projection, if the payments may stop.

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
for exporting during grid events. The total counts towards **Return on investment**. It is kept separate
from the cost figures, which stay as what your supplier charges.

- **By hand**: open **Add income or change settings**, and enter the date and amount.
  Choose whether it **counts as** regular (it carries on, such as Axle's monthly
  payment) or one-off (a referral, a bonus). Anything described as Axle starts as regular,
  everything else as one-off; **Edit** an entry to change it. Each entry shows which it is.
  Recorded Axle events are always regular.
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
- **Follow published prices**: choose your region and the tariff under "Or follow its
  published prices". Each half hour is then priced at what Octopus published for it at
  the time: every half hour for Agile, or whatever was in force that day for a variable
  tariff such as Flexible Octopus, so past price changes are included. The rates typed in
  cover any half hour without a published price, and the standing charge is always the one
  typed in.

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

## Tariff switch planner

Choose a tariff saved under **Compare tariffs** to see the last twelve months on it, month
by month, beside what you paid. Each month has three figures for the other tariff:

- **As it happened** replays your usage unchanged.
- **Charging moved** shifts battery and car charging to the cheapest half hours, without
  checking the battery would last. It is a best case.
- **Planned charging** runs the household again on that tariff. The house and the panels
  behave exactly as recorded; the battery and the car charge from the grid in the tariff's
  cheap times, and the battery then runs the house until it is empty. This is the realistic
  figure.

**Planned, against yours** compares planned charging with the same model run on your own
tariff. A model never matches real life exactly (the note under the table shows how far it
is from what you paid), so comparing the model with itself is the fair test. Read it as the
likely difference, not an exact bill.

### The charging plan

Above the table, the plan says when the battery and the car would charge on the new tariff,
how often the battery would run out before its next cheap time, for how long, how much
would be bought at dearer rates as a result, and which month is hardest.

- A time counts as cheap when its price is in the bottom quarter of that day's range.
- The plan does not hold back charging before a sunny day, so it exports a little more
  than a smart system would.
- **Battery used for the plan**: the usable capacity and fastest charging rate are worked
  out from your readings. Enter your own if they look wrong, or to see what a bigger
  battery would do on that tariff.

## Rate reminders

Under **Tariff rates**, a fixed-price deal can be given the last day its price is
guaranteed (**Price fixed until**). From 30 days before, a notice at the top of the page
says when it ends; after that date it says costs are still on the old rates. The notice
goes once the rates that follow have been entered. A notice also appears when half-hourly
prices could not be fetched.

## Backup and restore

**Download settings** saves everything you have entered as one file: tariff rates, tariffs
to compare, bills, extra income, notes, and the settings for return on investment, the planner
and folded sections.

To restore, choose the file and press **Check file**. The page lists what the file holds;
nothing changes until you press **Replace my settings with this file** and confirm.
Restoring replaces all of the above with the file's contents and removes anything not in
it. It is all or nothing: a damaged file changes nothing.

- Readings are not in the file. They are kept by Home Assistant's own backups, which also
  include these settings, so this file is mainly for moving to another install or keeping
  a copy before a big change.
- The file contains your bill figures and costs. It is saved to the device you download it
  on and sent nowhere else; keep it somewhere private.
- A file saved by a newer version of the app cannot be restored into an older one.

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
- The app only reads from Home Assistant. It never changes a device or setting. The
  outdoor temperature is read from Home Assistant too, not from an outside weather service.
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
