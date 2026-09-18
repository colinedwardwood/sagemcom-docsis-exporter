# Sagemcom DOCSIS cable modem exporter

Prometheus exporter for Sagemcom F3896/FAST3896S cable gateways (tested on
Cogeco's F3896 in bridge mode). It scrapes the modem's authenticated JSON-RPC
management API and exposes DOCSIS, Ethernet, and system telemetry on
`:9488/metrics`.

This firmware redirects the documented F3896 REST URLs back to the GUI. This
exporter talks to the same `/cgi/json-req` endpoint the web UI uses instead.

It never calls reboot, reset, configuration, or other write methods.

## Should this work on your modem/ISP?

Probably, if you have a Sagemcom **F3896** or **FAST3896S** cable gateway —
this is Sagemcom's general DOCSIS 3.1 cable-gateway hardware, not a
Cogeco-exclusive device. It's confirmed built and tested against Cogeco's
firmware; Breezeline (US, formerly Atlantic Broadband) deploys the same
hardware under their own firmware build and is a likely (untested) fit. If
you try it on another ISP's F3896/FAST3896S and it works (or needs a tweak),
please open an issue or PR — that's exactly the kind of report that makes
this more broadly useful.

The underlying `/cgi/json-req` JSON-RPC API (and its `XMO_*` error codes,
`tr181` namespace) is also shared much more broadly across Sagemcom's F@st
router family, which ISPs white-label for fibre/DSL gateways too — e.g. Bell's
Home Hub, Proximus's b-box, BT's Smart Hub. If your device is one of those
non-DOCSIS F@st routers rather than a cable modem, see
[`sagemcom_fast_exporter`](https://github.com/hairyhenderson/sagemcom_fast_exporter)
(by a colleague of mine at Grafana Labs) — it targets that broader fibre-hub
family generically. This exporter is narrower and DOCSIS-specific: cable
channel lock/power/SNR/codewords, thermal throttle, and the other cable-modem
telemetry Sagemcom's generic F@st API doesn't cover.

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
An Alloy scrape fragment lives in [`alloy/sagemcom-docsis.alloy`](alloy/sagemcom-docsis.alloy)
(**60s** interval, **50s** timeout).

`/healthz` only checks that the exporter process is up. Use
`sagemcom_docsis_scrape_up` (or `sagemcom_api_up` when
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
| `COLLECT_MODEM_LOG` | `true` | Copy new lines from the modem's RAM system log into the exporter's own log as `modem-log: ...` (see below) |
| `MODEM_LOG_MAX_LINES_PER_SCRAPE` | `200` | Cap on lines emitted per scrape; the newest are kept |
| `CHECK_API_LOGIN` | `false` | Extra login-only probe (second session). Leave off unless debugging auth |
| `SCRAPE_BUDGET_SECONDS` | `25` | Hard ceiling for modem HTTP work; keep below scrape_timeout |
| `EXPORT_DEVICE_IDENTIFIERS` | `false` | When `true`, export modem `serial` and Ethernet `mac` labels |

## Modem log capture

The modem keeps its log in a 1 MB RAM buffer that a reboot wipes, and remote syslog is
ISP-locked. So each scrape also reads `Device/DeviceInfo/SimpleLogs/SystemLog` (the
path the GUI's Logs page uses) in the same session and logs lines it has not seen
before as `modem-log: <line>`. Under systemd these land in journald, and Alloy ships the
journal to Loki, so the log survives a modem reboot. Query e.g.
`{unit="sagemcom-docsis-exporter"} |= "modem-log:"`.

Limits: nothing is captured while the modem's API is hung (scrapes fail), so you get
the lines up to the hang, not during it. Verbosity follows the modem's `LogLevel`
(currently `Warning`), and a failing log read never fails the metrics scrape.

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

If you're importing the dashboard by hand into a Grafana instance that isn't
using `deploy.sh` (e.g. via **Dashboards → New → Import**), use
[`sagemcom-docsis-modem.shared.json`](Grafana/dashboards/sagemcom-docsis-modem.shared.json)
instead of the plain `sagemcom-docsis-modem.json` — it templates the
datasource as an input (`${DS_PROMETHEUS}`) so Grafana prompts you to pick
your own Prometheus datasource rather than assuming the `grafanacloud-prom`
UID this repo's own Grafana Cloud stack uses.

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

Unofficial. Not affiliated with Cogeco, Breezeline, or Sagemcom. Use on
equipment you administer. Credentials stay in `.env` (gitignored) or your
secret store.
