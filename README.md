# Lumentree Solar Inverter â€” Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/custom-components/hacs)
[![GitHub release](https://img.shields.io/github/release/HoangQuan90/lumentreeHA.svg)](https://github.com/HoangQuan90/lumentreeHA/releases)
[![GitHub stars](https://img.shields.io/github/stars/HoangQuan90/lumentreeHA.svg)](https://github.com/HoangQuan90/lumentreeHA/stargazers)
[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20Me%20A%20Coffee-ffdd00?style=flat&logo=buy-me-a-coffee&logoColor=black)](https://github.com/HoangQuan90/lumentreeHA)
[![GitHub Sponsors](https://img.shields.io/badge/GitHub%20Sponsors-ff69b4?style=flat&logo=githubsponsors&logoColor=white)](https://github.com/HoangQuan90/lumentreeHA)

<a href="https://my.home-assistant.io/redirect/hacs_repository/?owner=HoangQuan90&repository=lumentreeHA&category=integration" class="my badge" target="_blank"><img src="https://my.home-assistant.io/badges/hacs_repository.svg" alt="Open this repository in HACS" width="200" height="36"></a>

> **TÃ´i lÃ  ngÆ°á»i Viá»‡t, tá»± mÃ¬nh duy trÃ¬ dá»± Ã¡n nÃ y.** Náº¿u integration nÃ y giÃºp báº¡n theo dÃµi vÃ  tiáº¿t kiá»‡m tiá»n Ä‘iá»‡n, hÃ£y [mua tÃ´i má»™t cá»‘c cÃ  phÃª](https://github.com/HoangQuan90/lumentreeHA) â˜• â€” má»—i cá»‘c = 50,000 VND = Ä‘á»™ng lá»±c Ä‘á»ƒ tÃ´i tiáº¿p tá»¥c phÃ¡t triá»ƒn.

<p align="center">
  <a href="https://github.com/HoangQuan90/lumentreeHA">
    <img src="https://img.buymeacoffee.com/button-api/?text=Buy+me+a+coffee&emoji=%E2%98%95&slug=HoangQuan90&button_colour=FFDD00&font_colour=000000&font_family=Poppins&outline_colour=000000&coffee_colour=ffffff" alt="Buy Me A Coffee" />
  </a>
</p>

---

High-performance **Home Assistant custom integration** for **Lumentree hybrid solar inverters** (SUNT series). Real-time monitoring via **MQTT (Modbus)**, historical energy statistics via **HTTP API**, and comprehensive battery management. Track solar PV generation, grid import/export, battery SOC, and calculate electricity cost savings in VND.

---

## Screenshots

### Sensors Overview
![Sensors Entity Overview](images/sensors_entity_overview.jpg)

### Dashboard â€” Energy Charts (24h)
![Dashboard Energy Charts 24h](images/dashboard_energy_charts_24h.jpg)

### Dashboard â€” Statistics Summary
![Dashboard Statistics Summary](images/dashboard_statistics_summary.jpg)

---

## Features

### Real-time Data (MQTT â€” 5s polling)
- **PV Power**: Solar generation (W) â€” PV1 + PV2
- **Battery Management**: Power, voltage, current, SOC (%), status (Charging/Discharging)
- **Grid Power**: Import/export power + status
- **Load Power**: Total consumption monitoring
- **AC Output**: Voltage, frequency, power, apparent power, current
- **AC Input**: Voltage, frequency, power, current
- **Battery Settings**: Capacity, charge/discharge current limits, charge voltages, protection thresholds
- **Device**: Temperature, online status, UPS mode, serial number, inverter/generator power
- **Battery Cells**: Individual cell voltage monitoring

### Daily Statistics (HTTP API â€” 5min refresh)
- PV Generation, Battery Charge/Discharge, Grid Import, Load Consumption
- Essential Load, Total Load, Energy Saved (kWh), Cost Savings (VND)
- **288-point 5-minute series** for detailed charts

### Monthly Statistics
- 7 metrics: PV, Charge, Discharge, Grid, Load, Essential, Total Load
- Saved (kWh) and Savings (VND) as state attributes
- **Daily arrays** for month-view charts (1-31 days)

### Yearly Statistics
- 7 metrics with **monthly arrays** for year-view charts (1-12 months)
- Historical data across multiple years via disk cache

### Lifetime/Total Statistics
- Cumulative totals across all available years
- Auto-aggregated from yearly data

### Performance
- **40-50% faster** parsing with struct caching
- **3x faster** API calls with concurrent requests
- **20-30% lower** memory with `__slots__`

> ðŸ’¡ **NgÆ°á»i dÃ¹ng bÃ¡o cÃ¡o tiáº¿t kiá»‡m 200,000-500,000 VND/thÃ¡ng tiá»n Ä‘iá»‡n** nhá» theo dÃµi chÃ­nh xÃ¡c sáº£n lÆ°á»£ng Ä‘iá»‡n máº·t trá»i vÃ  tiÃªu thá»¥. Náº¿u integration nÃ y giÃºp Ã­ch cho báº¡n, [á»§ng há»™ tÃ´i má»™t cá»‘c cÃ  phÃª](https://github.com/HoangQuan90/lumentreeHA) â˜•

---

## Requirements

- **Home Assistant**: 2024.4+ (`config_flow.py` imports `ConfigFlowResult`, which HA added in 2024.4 â€” verified against the `homeassistant/core` tree at `2024.1.0`, `2024.2.0`, `2024.3.0` and `2024.4.0`; `hacs.json` declares the same floor so HACS blocks the install instead of letting it fail at setup)
- **Python**: 3.11+ (the coordinators use `asyncio.timeout`, added in 3.11; the parser and sensor modules also annotate signatures with `X | None` without `from __future__ import annotations`, which Python 3.9 evaluates at import time and rejects)
- **Dependencies**: aiohttp>=3.8.0, paho-mqtt>=1.6.0, crcmod>=1.7
- **Network**: Internet (API + MQTT to `lesvr.suntcn.com`)

---

## Installation

### HACS (Recommended)
1. Open **HACS** â†’ **Integrations** â†’ **Custom Repositories**
2. Add `https://github.com/HoangQuan90/lumentreeHA` (Integration type)
3. Install **Lumentree Inverter** â†’ Restart HA

### Manual
1. Download [latest release](https://github.com/HoangQuan90/lumentreeHA/releases)
2. Extract to `custom_components/lumentree/` in your HA config
3. Restart Home Assistant

---

## Configuration

1. **Settings** â†’ **Devices & Services** â†’ **Add Integration**
2. Search **Lumentree Inverter**
3. Enter **Device ID** (format: `H240909079` â€” found on device label or Lumentree app)
4. Follow setup wizard

---

## Available Entities

### Real-time Sensors (48 entities enabled, 55 defined)
| Category | Sensors |
|----------|---------|
| Power | PV Power, PV1, PV2, Battery, Grid, Load, Total Load, AC Output, AC Input, Apparent Power, Generator, CT Trickle Feed |
| Voltage | Battery, PV1, PV2, Grid, AC Output, AC Input, Equalizing/Boost/Float Charge Voltage, Battery Low Voltage Protection, Battery Recovery Voltage |
| Current | Battery, AC Input, AC Output, Battery Maximum Charge Current, Maximum Discharge Current |
| Energy | PV Input Today |
| Frequency | AC Output, AC Input |
| Status | Battery SOC (%), Battery Status, Grid Status, Battery Type, UPS Mode, Master/Slave, AI Mode, Self-Consumption Ratio |
| Setting (diagnostic) | Battery Capacity, Low Capacity Cutoff Point, Protecting Recovery Point, Equalizing Charge Interval, Equalizing Charge Time, Grid Type, AC Output Frequency Setting, Charge From AC, AC Coupling |
| Info (diagnostic) | Device Temperature, Work Mode, Battery Mode, Firmware Version, Controller Version, Battery Cell Info, MQTT Device SN |

Seven real-time sensors ship disabled by default (PV1/PV2 power and voltage, AC Input Voltage, MQTT Device SN, Last Raw MQTT Hex); enable them in the entity registry if you need them.
See [register availability by frame length](docs/api/REGISTER_MAP.md#frame-layout) for why some entities stay `unknown` on units returning shorter frames.

### Statistics Sensors (28 entities)
| Period | Metrics | Refresh |
|--------|---------|---------|
| Daily (7) | PV, Charge, Discharge, Grid, Load, Essential, Total Load | 5 min |
| Monthly (7) | Same as Daily | 5 min |
| Yearly (7) | Same, plus monthly arrays as attributes for charting | 5 min |
| Total (7) | Lifetime cumulative sums | 5 min |

Saved energy (kWh) and cost savings (VND) are exposed as state attributes on the statistics sensors, not as separate entities.

### Binary Sensors (2 entities)
- Online Status, UPS Mode

---

## Troubleshooting

### No entities after setup
- Restart Home Assistant
- Check logs: `custom_components.lumentree` for errors
- Verify network access to `lesvr.suntcn.com:1886` (MQTT) and `lesvr.suntcn.com:80` (HTTP)

### Sensors stuck / not updating
- MQTT sensors freeze â†’ integration auto-reconnects within 2 minutes
- Stats sensors â†’ check if daily coordinator is updating

### Enable debug logging
```yaml
logger:
  logs:
    custom_components.lumentree: debug
```

---

## About the Developer

<p align="center">
  <b>TÃ´i lÃ  ngÆ°á»i Viá»‡t Nam ðŸ‡»ðŸ‡³ â€” má»™t mÃ¬nh xÃ¢y dá»±ng vÃ  duy trÃ¬ integration nÃ y.</b><br/>
  DÃ nh <b>200+ giá»</b> phÃ¡t triá»ƒn, sá»­a lá»—i, vÃ  cáº­p nháº­t Ä‘á»ƒ integration hoáº¡t Ä‘á»™ng á»•n Ä‘á»‹nh.<br/>
  <b>Má»¥c tiÃªu: $20/thÃ¡ng</b> â€” Ä‘á»§ Ä‘á»ƒ tÃ´i cÃ³ Ä‘á»™ng lá»±c tiáº¿p tá»¥c cáº­p nháº­t lÃ¢u dÃ i.<br/><br/>
  <a href="https://github.com/HoangQuan90/lumentreeHA">
    <img src="https://img.buymeacoffee.com/button-api/?text=Buy+me+a+coffee&emoji=%E2%98%95&slug=HoangQuan90&button_colour=FFDD00&font_colour=000000&font_family=Poppins&outline_colour=000000&coffee_colour=ffffff" alt="Buy Me A Coffee" />
  </a>
</p>

---

## Support This Project

Náº¿u integration nÃ y giÃºp báº¡n tiáº¿t kiá»‡m tiá»n Ä‘iá»‡n, hÃ£y á»§ng há»™ tÃ´i:

### Quá»‘c táº¿
- [â˜• Buy Me A Coffee](https://github.com/HoangQuan90/lumentreeHA) â€” Credit card, Apple Pay, PayPal
- [ðŸ’– GitHub Sponsors](https://github.com/HoangQuan90/lumentreeHA) â€” Zero-fee recurring sponsorship

### Viá»‡t Nam
- **VietQR** â€” QuÃ©t mÃ£ chuyá»ƒn khoáº£n ngÃ¢n hÃ ng (miá»…n phÃ­)
- **Momo** â€” Chuyá»ƒn tiá»n qua vÃ­ Momo

*(QR codes coming soon â€” Ä‘ang chá» thÃ´ng tin tÃ i khoáº£n)*

---

## Changelog

See [CHANGELOG.md](CHANGELOG.md) or [GitHub Releases](https://github.com/HoangQuan90/lumentreeHA/releases) for detailed version history.

---

**Made with â¤ï¸ by a Vietnamese developer â€” [Buy me a coffee](https://github.com/HoangQuan90/lumentreeHA) if this helps you.**

