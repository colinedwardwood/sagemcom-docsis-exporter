# Metrics and API reference

Prometheus metrics, alert thresholds, and the Sagemcom JSON-RPC protocol this
exporter uses. Implementation: [`exporter.py`](exporter.py). Alerts:
[`Grafana/alerts/cogeco-alerts.json`](Grafana/alerts/cogeco-alerts.json).
Transport/RPC specs: [`openapi.yaml`](openapi.yaml), [`openrpc.json`](openrpc.json).

---

## Prometheus metrics

### Reachability and scrape health

| Metric | Type | Labels | Notes |
|--------|------|--------|-------|
| `cogeco_sagemcom_gui_up` | gauge | | `1` if `https://<ADDRESS>/2.0/gui/` answers |
| `cogeco_sagemcom_gui_request_duration_seconds` | gauge | | GUI response time |
| `cogeco_sagemcom_api_up` | gauge | | Only when `CHECK_API_LOGIN=true` (extra login) |
| `cogeco_sagemcom_api_request_duration_seconds` | gauge | | Login duration; `CHECK_API_LOGIN=true` only |
| `cogeco_sagemcom_docsis_scrape_up` | gauge | | `1` if the batched TR-181 scrape succeeded |
| `cogeco_sagemcom_logout_success` | gauge | | `1` if session logout succeeded |
| `cogeco_sagemcom_scrape_error_info` | gauge | `stage`, `error` | Latest failure detail |
| `cogeco_sagemcom_scrape_duration_seconds` | gauge | | Full `/metrics` wall time |
| `cogeco_sagemcom_internet_probe_up` | gauge | `target` | TCP connect from the exporter host |
| `cogeco_sagemcom_internet_probe_duration_seconds` | gauge | `target` | TCP connect latency |

Public probes run **outside** the modem session lock. Treat them as a symptom,
not proof of an ISP outage.

Modem work is capped by `SCRAPE_BUDGET_SECONDS` (default 25). Keep this below
your Prometheus/Alloy `scrape_timeout` (Alloy fragment uses 60s interval / 50s
timeout).

### System and identity

| Metric | Type | Labels | Notes |
|--------|------|--------|-------|
| `cogeco_sagemcom_uptime_seconds` | gauge | | Decrease ⇒ modem restart |
| `cogeco_sagemcom_modem_info` | gauge | `model`, `software`, `hardware`, optional `serial` | `serial` only if `EXPORT_DEVICE_IDENTIFIERS=true` |
| `cogeco_sagemcom_reboot_count` | gauge | | Lifetime reboot counter from DeviceInfo |
| `cogeco_sagemcom_cpu_usage_percent` | gauge | | Modem-reported CPU |
| `cogeco_sagemcom_load_average` | gauge | `window`=`1m`/`5m`/`15m` | |
| `cogeco_sagemcom_memory_bytes` | gauge | `type`=`total`/`free` | Firmware KiB values converted to bytes |
| `cogeco_sagemcom_temperature_celsius` | gauge | `sensor`, `status`, `enabled` | Placeholder readings below -40°C are dropped |
| `cogeco_sagemcom_thermal_throttle` | gauge | | DOCSIS thermal throttle state |

### DOCSIS

| Metric | Type | Labels | Notes |
|--------|------|--------|-------|
| `cogeco_sagemcom_docsis_registered` | gauge | | `1` when status looks online |
| `cogeco_sagemcom_docsis_registration_info` | gauge | `status` | Status string on an info series |
| `cogeco_sagemcom_docsis_channel_info` | gauge | `direction`, `channel`, `modulation` | Modulation lives here, not on samples |
| `cogeco_sagemcom_docsis_channel_lock` | gauge | `direction`, `channel` | Omitted when lock state is unknown |
| `cogeco_sagemcom_docsis_channel_frequency_hertz` | gauge | `direction`, `channel` | |
| `cogeco_sagemcom_docsis_channel_power_dbmv` | gauge | `direction`, `channel` | DS and US power |
| `cogeco_sagemcom_docsis_downstream_snr_db` | gauge | `direction`, `channel` | Downstream only |
| `cogeco_sagemcom_docsis_downstream_codewords_total` | counter | `direction`, `channel`, `type` | Omitted when the field is missing |

Practical RF targets for QAM256 plant:

- Downstream power: ideally -7 to +7 dBmV (wider plant tolerance ~-15 to +15)
- Downstream SNR: aim ≥ 33–35 dB
- Upstream power: comfortable below ~50 dBmV; sustained > 51 dBmV is a warning

Uncorrectable codewords are packet loss on coax. Use `rate(...[10m])`.

### Ethernet

| Metric | Type | Labels | Notes |
|--------|------|--------|-------|
| `cogeco_sagemcom_ethernet_up` | gauge | `alias`, `ifc`, `role` | Link state |
| `cogeco_sagemcom_ethernet_speed_mbps` | gauge | same | Negotiated rate |
| `cogeco_sagemcom_ethernet_info` | gauge | + `status`, `duplex`, optional `mac` | `mac` only with `EXPORT_DEVICE_IDENTIFIERS=true` |
| `cogeco_sagemcom_ethernet_bytes_total` | counter | + `direction` | |
| `cogeco_sagemcom_ethernet_packets_total` | counter | + `direction` | |
| `cogeco_sagemcom_ethernet_errors_total` | counter | + `direction` | |
| `cogeco_sagemcom_ethernet_discards_total` | counter | + `direction` | |

`direction` is `receive` or `transmit`.

---

## Alert thresholds

Shipped in `Grafana/alerts/cogeco-alerts.json`:

| Alert | Expression | For | Severity |
|-------|------------|-----|----------|
| Modem GUI unreachable | `cogeco_sagemcom_gui_up < 1` | 3m | critical |
| DOCSIS scrape failed | `cogeco_sagemcom_docsis_scrape_up < 1` | 5m | warning |
| Internet path probes down | `min(cogeco_sagemcom_internet_probe_up) < 1` | 5m | critical |
| Modem restarted | `delta(cogeco_sagemcom_uptime_seconds[15m]) < -60` | — | warning |
| Downstream SNR low | `min(cogeco_sagemcom_docsis_downstream_snr_db) < 33` | 10m | warning |
| Upstream power high | `max(...power_dbmv{direction="upstream"}) > 51` | 10m | warning |
| Uncorrectable codewords rising | `sum(rate(...codewords_total{type="uncorrectable"}[10m])) > 0` | 10m | warning |
| DOCSIS channel unlocked | `min(cogeco_sagemcom_docsis_channel_lock) < 1` | 5m | warning |
| Exporter metrics stale | `time() - timestamp(cogeco_sagemcom_gui_up) > 900` | — | critical |

GUI-down uses `noDataState: NoData` so a missing exporter does not double-page with the stale rule.

---

## JSON-RPC protocol

### HTTP transport

- URL: `https://<ADDRESS>/cgi/json-req`
- Method: `POST`
- Content-Type: `application/x-www-form-urlencoded`
- Body: single form field `req` whose value is the JSON envelope string

### Auth

SHA-512 challenge-response, matching the web UI:

```
password_hash = SHA512(PASSWORD)
ha1           = SHA512(USERNAME + ":" + session_nonce + ":" + password_hash)
auth_key      = SHA512(ha1 + ":" + request_id + ":" + cnonce + ":JSON:/cgi/json-req")
```

On the initial login, `session-id` is `"0"` and `session_nonce` is empty.
After each reply, the client adopts `reply.nonce` when present and increments
`request_id`.

Success error code is `16777238` (`XMO_NO_ERR`).

### Namespace requirement

`session-options.nss` must be an object list:

```json
[
  {"name": "gtw", "uri": "http://sagemcom.com/gateway-data"},
  {"name": "tr181", "uri": "http://sagemcom.com/tr181-data"}
]
```

The string form `"gtw:tr181"` authenticates but every later `getValue` returns
`XMO_UNKNOWN_PATH_ERR` on this firmware.

### Scrape lifecycle

1. `logIn` — receive `session-id` and `nonce`
2. One multi-action `getValue` batch for all configured TR-181 paths
3. `logOut` (failures set `logout_success=0` and `scrape_error_info`)

Modem sessions are serialized with a lock. Public TCP probes are not.

### TR-181 paths

| Path | Used for |
|------|----------|
| `Device/Docsis/CableModem/Downstreams` | Lock, frequency, power, SNR, codewords |
| `Device/Docsis/CableModem/Upstreams` | Lock, frequency, upstream power |
| `Device/DeviceInfo/UpTime` | Uptime seconds |
| `Device/Docsis/CableModem/Status` | Registration string |
| `Device/Ethernet/Interfaces` | LAN link/stats (`COLLECT_ETHERNET`) |
| `Device/DeviceInfo/ModelName` | Identity (`COLLECT_SYSTEM`) |
| `Device/DeviceInfo/SoftwareVersion` | Identity |
| `Device/DeviceInfo/HardwareVersion` | Identity |
| `Device/DeviceInfo/SerialNumber` | Identity (label export optional) |
| `Device/DeviceInfo/RebootCount` | Reboot counter |
| `Device/DeviceInfo/MemoryStatus` | Total/free KiB |
| `Device/DeviceInfo/ProcessStatus` | CPU + load averages |
| `Device/DeviceInfo/TemperatureStatus` | Sensors |
| `Device/Docsis/CableModem/ThermalThrottleState` | Throttle flag |
