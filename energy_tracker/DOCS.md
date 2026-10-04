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
