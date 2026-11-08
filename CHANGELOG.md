### [v1.2.0](https://github.com/yatsyshynoleksandr/power-monitor/tree/v1.2.0) (2026-11-09)

*Fix :*

- day/week boundaries and the report time now use local time (`TIMEZONE`, default `Europe/Kyiv`)
  instead of the container's UTC; DST-aware
- a daily report covers exactly 00:00–00:00 (no more "23г 59хв 59с" / "1д 3с"): periods close
  exactly at local midnight and an iteration crossing it is split between the two days
- no proportional "normalization" any more: downtime is credited to the last known state
- reports that were due while the monitor was down are sent later instead of being lost
- a confirmed state change is dated to its first observation, not to the end of the debounce
- grammar: "Інтернет зник (світла немає)"

*Change :*

- `REPORT_HOUR` default 7 → 9 (local time); reports are sent exactly on the hour
- clearer emoji: 📵 internet down / 📶 internet back
- daily report shows a single date with the weekday, e.g. "19.09 (пт)"
- `<0.1%` / `>99.9%` instead of a misleading `0.0%` / `100.0%`
- internet outage count in reports; "Перерва в моніторингу" in the startup message
- durations up to 48h shown in hours ("24г")
- stats file format v2 (old files are migrated automatically)

*Add :*

- `HEALTHCHECK` (heartbeat file), unit tests, multi-arch image (amd64 + arm64)

### [v1.1.0](https://github.com/yatsyshynoleksandr/power-monitor/tree/v1.1.0) (2026-07-17)

*Add :*

- optional webhook push of power/internet state to the Mango Home dashboard
  (`MANGO_WEBHOOK_URL` / `MANGO_WEBHOOK_TOKEN`): on every state change, on startup,
  and as a ~1-minute heartbeat

### [v1.0.0](https://github.com/yatsyshynoleksandr/power-monitor/tree/v1.0.0) (2026-06-12)

*Add :*

- init power-monitor application
