# Xizhi LiDAR Weather Monitor

Small Python monitor for finding LiDAR-worthy weather windows in Xizhi District, New Taipei City (`新北市汐止區`).

It uses the Central Weather Administration Open Data API, sends Telegram notifications for special weather only, and sends a configurable regular summary even when nothing special happened.

## What It Alerts On

Immediate Telegram alerts are sent for:

- Torrential rain level and above: `豪雨`, `大豪雨`, `超大豪雨`.
- Short-duration intense rainfall: `短時強降雨`, `短延時強降雨`, `劇烈降雨`.
- CWA thunderstorm nowcast / immediate thunderstorm warning.
- Actual nearby lightning observation within `LIGHTNING_ALERT_RADIUS_KM` and `LIGHTNING_ALERT_MINUTES`.
- Typhoon or tropical cyclone wording.
- Dense fog: `濃霧`.
- Numeric low visibility at or below `VISIBILITY_ALERT_M`.
- Numeric strong wind at or above `WIND_ALERT_MPS` or `BEAUFORT_ALERT`.
- Wet ground after sustained rain, using rain observations accumulated in `state.json`.
- Matching CWA warning feeds that mention New Taipei or Xizhi.

Light rain, normal rain, and `大雨` alone are not immediate alerts. They still appear in the regular summary.

Forecast-only thunderstorm wording, such as `午後雷陣雨`, is summary-only. It does not trigger an immediate Telegram alert unless there is a CWA immediate thunderstorm warning or actual nearby lightning observation.

## Data Sources

- CWA Open Data API base: `https://opendata.cwa.gov.tw/api/v1/rest/datastore`
- New Taipei township 3-day forecast dataset: `F-D0047-069`
- Default location filter: `LocationName=汐止區`
- Warning feeds are configured in `.env` through `CWA_WARNING_DATASET_IDS`.
- Lightning observation data is configured through `CWA_LIGHTNING_DATASET_ID`.
- Rain observation data is configured through `CWA_RAIN_DATASET_IDS`.

CWA dataset references:

- [New Taipei township 3-day forecast, F-D0047-069](https://opendata.cwa.gov.tw/dataset/forecast/F-D0047-069)
- [Lightning observation, O-A0039-001](https://opendata.cwa.gov.tw/dataset/observation/O-A0039-001)
- [CWA warning datasets](https://opendata.cwa.gov.tw/dataset/warning?page=1)
- [CWA API documentation](https://opendata.cwa.gov.tw/dist/opendata-swagger.html)

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
cp .env.example .env
```

The monitor uses only Python's standard library, so there are no packages to install.

Edit `.env` and fill in:

- `CWA_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

## Run

Preview without Telegram:

```bash
python weather_monitor.py --dry-run --summary-now
```

Run normally:

```bash
python weather_monitor.py
```

Force the regular summary immediately:

```bash
python weather_monitor.py --summary-now
```

Run as an interactive Telegram bot:

```bash
python weather_monitor.py --bot
```

In Telegram, send one of these messages to the bot:

```text
/weather
即時天氣
天氣
天氣 臺北市 信義區
/weather 新北市 汐止區
地區設定
地區 新北市 汐止區
設定地區 臺北市 信義區
摘要設定
摘要每 6 小時
摘要每 90 分鐘
摘要起始 08:00
```

The bot replies with the latest Xizhi weather summary and whether any LiDAR immediate-notification condition is active.

`地區 ...` changes the monitored township. Chat-configured locations use the CWA all-township forecast dataset `F-D0047-093` and are saved in `state.json`.

`天氣 ...` or `/weather ...` with a county and township performs a one-time weather query for that location without changing the monitored township.

`摘要每 ...` changes how often the regular summary is sent. `摘要起始 HH:MM` aligns the schedule to a fixed Taiwan time. For example, `摘要起始 08:00` plus `摘要每 6 小時` sends around 08:00, 14:00, 20:00, and 02:00. Chat settings are saved in `state.json` and override `.env`.

## State And Duplicate Alerts

The monitor writes `state.json`.

- `sent_alerts` prevents repeat Telegram alerts for the same weather event.
- `last_summary_at` controls the regular summary cadence.
- `summary_settings` stores Telegram-configured summary interval and start time.
- `location_settings` stores Telegram-configured county and township.
- Sent alert records are pruned after 14 days.

Delete `state.json` only when you intentionally want the monitor to forget prior alerts and summary timing.

## Cron Setup

Run the monitor every 15 minutes. It will send immediate alerts only for new LiDAR-worthy weather and will send the summary only when the configured interval has passed.

Open your crontab:

```bash
crontab -e
```

Add this line, adjusting the project path if needed:

```cron
*/15 * * * * cd /Users/kuchifeng/Documents/Codex/2026-05-30/create-a-python-weather-monitoring-project && /Users/kuchifeng/Documents/Codex/2026-05-30/create-a-python-weather-monitoring-project/.venv/bin/python weather_monitor.py >> weather_monitor.log 2>&1
```

Optional: send a guaranteed summary at fixed Taiwan times, for example 00:00, 08:00, and 16:00:

```cron
0 0,8,16 * * * cd /Users/kuchifeng/Documents/Codex/2026-05-30/create-a-python-weather-monitoring-project && /Users/kuchifeng/Documents/Codex/2026-05-30/create-a-python-weather-monitoring-project/.venv/bin/python weather_monitor.py --summary-now >> weather_monitor.log 2>&1
```

Use either the every-15-min cadence by itself, or add the fixed summary line if you want summaries anchored to exact wall-clock times.

## Docker Setup

The project includes a `Dockerfile` and `docker-compose.yml`.

Copy the project to your NAS Docker folder, then create `.env` from `.env.example`:

```bash
cp .env.example .env
```

Create the state file before the first container start:

```bash
touch state.json
```

Build and run the long-running Telegram bot service:

```bash
docker compose up -d --build
```

Run a one-time summary from Docker:

```bash
docker compose run --rm xizhi-lidar-weather python weather_monitor.py --summary-now
```

If you do not run the long-running bot service, schedule this command from NAS cron or Task Scheduler every 15 minutes:

```bash
docker compose run --rm xizhi-lidar-weather python weather_monitor.py
```

If you use the default Docker service, it already runs `python weather_monitor.py --bot`, which both listens for Telegram weather questions and checks proactive alerts about every 15 minutes. Use NAS cron only if you prefer not to keep the bot service running.

Do not run the default bot service and a cron job against the same `state.json` at the same time. The state file has a lock, but using one scheduler is cleaner.

## Telegram Notes

1. Create a bot with Telegram BotFather.
2. Put the bot token in `TELEGRAM_BOT_TOKEN`.
3. Send a message to the bot or group.
4. Retrieve the chat id and put it in `TELEGRAM_CHAT_ID`.

For groups, add the bot to the group first. The chat id is usually negative for groups.
