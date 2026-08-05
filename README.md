# Cogeco Sagemcom exporter

Prometheus exporter for Cogeco Sagemcom cable modems (tested on the F3896 in
bridge mode). It scrapes the modem's authenticated JSON-RPC management API and
exposes DOCSIS, Ethernet, and system telemetry on `:9488/metrics`.

Cogeco's firmware redirects the documented F3896 REST URLs back to the GUI.
This exporter talks to the same `/cgi/json-req` endpoint the web UI uses.

It never calls reboot, reset, configuration, or other write methods.

## What it exposes

| Area | Examples |
|------|----------|
| Reachability | GUI up/latency, scrape errors, logout success, public TCP probes |
| DOCSIS | Channel lock/power/SNR, codeword counters, registration, uptime |
| Ethernet | Link up, negotiated speed, bytes/packets/errors/discards |
| System | Memory, CPU/load, temperature, reboot count, thermal throttle |

Full metric reference, alert thresholds, and protocol notes:
[`METRICS_AND_API.md`](METRICS_AND_API.md). Machine-readable transport/RPC specs:
[`openapi.yaml`](openapi.yaml), [`openrpc.json`](openrpc.json).

## Quick start

```sh
cp .env.example .env
# set PASSWORD (and MODEM_USERNAME if not admin)

docker compose up --build -d
curl -s http://127.0.0.1:9488/healthz
curl -s http://127.0.0.1:9488/metrics | head
```

Point Prometheus or Grafana Alloy at `http://<exporter-host>:9488/metrics`.
An Alloy scrape fragment lives in [`alloy/cogeco-sagemcom.alloy`](alloy/cogeco-sagemcom.alloy)
(**60s** interval, **50s** timeout).

`/healthz` only checks that the exporter process is up. Use
`cogeco_sagemcom_docsis_scrape_up` (or `cogeco_sagemcom_api_up` when
`CHECK_API_LOGIN=true`) for modem/API health.

## Configuration

| Variable | Default | Notes |
|----------|---------|-------|
| `ADDRESS` | `192.168.100.1` | Bridge-mode management address |
| `MODEM_USERNAME` | `admin` | Prefer this over `USERNAME` — macOS/zsh already export `USERNAME` |
| `PASSWORD` | _(required if DOCSIS/API enabled)_ | Process exits on startup if missing |
| `LISTEN_ADDRESS` | `0.0.0.0` | Bind address; restrict at the host firewall if the LAN is untrusted |
| `LISTEN_PORT` | `9488` | Exporter port |
| `PROBE_TARGETS` | `1.1.1.1:443,8.8.8.8:443` | Comma-separated `host:port` TCP probes |
| `COLLECT_DOCSIS` | `true` | Login + channel/system scrape; set `false` for GUI/probes only |
| `COLLECT_ETHERNET` | `true` | Include Ethernet interface stats in the same session |
| `COLLECT_SYSTEM` | `true` | Include memory/CPU/temperature/identity |
| `CHECK_API_LOGIN` | `false` | Extra login-only probe (second session). Leave off unless debugging auth |
| `SCRAPE_BUDGET_SECONDS` | `25` | Hard ceiling for modem HTTP work; keep below scrape_timeout |
| `EXPORT_DEVICE_IDENTIFIERS` | `false` | When `true`, export modem `serial` and Ethernet `mac` labels |

## Firmware notes

Login `session-options.nss` must be the GUI's namespace object list (`gtw` +
`tr181` URIs). Passing the option string `"gtw:tr181"` still returns a session,
but every `getValue` then fails with `XMO_UNKNOWN_PATH_ERR`.

Each scrape uses one batched `getValue` request and always attempts `logOut`.
Modem sessions are serialized; public TCP probes run outside that lock. Reply
nonces are adopted when the firmware rotates them.

## Grafana

Dashboard and alert rules live in [`Grafana/`](Grafana/). Deploy to Grafana Cloud:

```sh
export GRAFANA_ORG_SLUG=<org-slug>
export GRAFANA_API_KEY=...   # or ~/.tokens/grafana-<org-slug>/grafana-api.key
./Grafana/deploy.sh
```

## Design choices

- **Python stdlib only** — no pip dependencies; small Alpine image
- **Read-only** — scrape path never mutates modem config
- **Fail soft** — target faults return HTTP 200 with `*_up=0` and `scrape_error_info`
- **Budgeted scrapes** — modem work stops at `SCRAPE_BUDGET_SECONDS`
- **Identity optional** — serial/MAC labels are off unless explicitly enabled

## Development

```sh
python3 -m unittest discover -s tests -t . -v
```

## Disclaimer

Unofficial. Not affiliated with Cogeco or Sagemcom. Use on equipment you
administer. Credentials stay in `.env` (gitignored) or your secret store.
