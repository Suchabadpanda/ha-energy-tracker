# Energy Tracker

> **Built for Sigenergy solar and battery systems.** It reads the sensors created by the
> Sigenergy integration for Home Assistant. Other inverters will not work without editing
> the sensor list (see "Using different sensors").

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

## Costs

- Costs come from the inverter's lifetime import and export counters, split into half-hour
  slots and priced with the rates that applied on each day. Expect small differences from
  your bill.
- Enter prices before VAT and set the VAT percentage, or enter prices including VAT and
  set VAT to 0. VAT is added to import and the standing charge, not to export.
- One cheap window per day (or a flat rate) is supported. Prices are shown in pounds and
  pence.
- "Saved by off-peak" is what the same import would have cost at the dearest rate, minus
  what it did cost.

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

Things to know:

- Your usage is replayed exactly as it happened. A tariff whose cheap window differs from
  yours will look dearer than it would be in practice, because your battery and car were
  charging to suit your current window.
- Looked-up prices are today's. They are not updated afterwards, and earlier months are
  priced at today's rates.
- The lookup covers Octopus Energy only, and leaves out tariffs whose prices change every
  half hour (Agile) or track the wholesale price. Other suppliers do not publish a price
  list that an app can read.
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
