# Barreauto

Small Python script that logs into Barreau de Paris website, fetches serment dates, and sends notifications through ntfy.

## What it does

- Authenticates with email/password (and optional TOTP).
- Fetches the full dates list and computes available slots.
- Prints a readable table in stdout (or JSON output).
- Sends:
  - a full run report to `NTFY_ALL_CHANNEL`
  - a high-priority alert to `NTFY_FREE_CHANNEL` when free slots exist

## Usage
### Requirements

- Python 3.10+
- `requests`

This repo is also `uv`-friendly (script metadata is embedded in `avocatparis_serment_dates.py`).

### Run locally

```bash
export EMAIL="you@example.com"
export PASSWORD="your-password"
# Optional
export AVOCATPARIS_TOTP="123456"
export NTFY_ALL_CHANNEL="your-all-channel"
export NTFY_FREE_CHANNEL="your-free-channel"
export NTFY_BASE_URL="https://ntfy.sh"

python avocatparis_serment_dates.py --timeout 30
```

You can also run with `uv`:

```bash
uv run avocatparis_serment_dates.py --timeout 30
```

### Useful flags

- `--json`: print full JSON result
- `--output /path/file.json`: write JSON output to file
- `--ntfy-all-channel <channel>`: override all-events channel
- `--ntfy-free-channel <channel>`: override free-slots channel
- `--totp <code>`: pass TOTP code directly

### Example output

```text
date       | ouvert | selectionnable | places libres | places totales | occupation %
-----------+--------+----------------+---------------+----------------+-------------
2026-08-24 | oui    | non            | 0             | 61             | 100.0
2026-08-25 | oui    | non            | 0             | 55             | 100.0
2026-08-26 | oui    | non            | 0             | 56             | 100.0
2026-09-14 | oui    | oui            | 0             | 40             | 100.0
2026-09-21 | oui    | oui            | 0             | 20             | 100.0
2026-09-28 | oui    | oui            | 0             | 20             | 100.0
```

## Deployment
### GitHub Actions

Workflow: `.github/workflows/serment-dates.yml`

- Scheduled runs are configured in UTC cron.
- Required secrets:
  - `EMAIL`
  - `PASSWORD`
  - `AVOCATPARIS_TOTP` (optional)
  - `NTFY_ALL_CHANNEL` (optional)
  - `NTFY_FREE_CHANNEL` (optional)

### Systemd scheduling (Raspberry Pi)

This repo includes a `systemd` timer setup to run in parallel with GitHub Actions.

Files:
- `systemd/barreauto.example.service`
- `systemd/barreauto.example.timer`
- `systemd/barreauto.env.example`

Schedule is adapted for a host running in UTC+2:
- `10:07`, `13:07`, `17:07`, `19:07` (local time)

#### Install

Make sure `uv` is installed on the Raspberry Pi first.

```bash
cp systemd/barreauto.env.example systemd/barreauto.env
# edit systemd/barreauto.env with your credentials/channels

cp systemd/barreauto.example.service systemd/barreauto.service
cp systemd/barreauto.example.timer systemd/barreauto.timer
# edit PATH_TO_PROJECT in systemd/barreauto.service

sudo cp systemd/barreauto.service /etc/systemd/system/
sudo cp systemd/barreauto.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now barreauto.timer
```

#### Check

```bash
systemctl status barreauto.timer
systemctl list-timers --all | grep barreauto
journalctl -u barreauto.service -n 100 --no-pager
```
