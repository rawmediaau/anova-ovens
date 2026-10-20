# Anova Ovens for Home Assistant

**Anova Ovens** (domain `anova_ovens`) is a Home Assistant integration for the Anova Precision Oven (APO) using Anova's [official developer API](https://developer.anovaculinary.com/docs/devices/wifi/oven-commands). Real-time telemetry via WebSocket (`wss://devices.anovaculinary.io`), plus basic control.

Based on [wrongdoug/ha-anova-precision-oven](https://github.com/wrongdoug/ha-anova-precision-oven) v1.3.0 (MIT), extended with v2 oven support, lamp control, and the oven camera. It uses its own domain, so it is a separate integration from the original: install one or the other, not both.

> **Provided as-is, with no warranty.** You are responsible for verifying correct behaviour, particularly when automating a physical cooking appliance. The app login and camera use Anova's unofficial app protocol and may stop working if Anova changes its app. Not affiliated with Anova.

## Scope

Full read-only telemetry for the oven (22 sensors and binary sensors). Limited control: on/off, target temperature, dry (oven) or wet (sous vide) mode on v2 ovens, and the lamp. Complex cooking (multi-stage recipes, steam, custom heating element combinations, timer, fan/vent) should be driven from the Anova app. See [Climate Entity Behavior](#climate-entity-behavior) before automating against the climate entity.

### Supported

- Anova Precision Oven v1 (tested by the original author; behaviour unchanged from v1.3.0, checked against its outputs)
- Anova Precision Oven v2 (tested on a v2 oven: sensors, cook start/stop, wet/dry presets, lamp, camera)

### Not Supported

Anova Precision Cooker (APC sous vide circulator). That's a different product with a different API -- use the built-in [Anova integration](https://www.home-assistant.io/integrations/anova) in HA core.

## Entities

### Sensors (17, read-only)

| Entity | Description |
|---|---|
| Dry temperature | Current dry bulb temperature |
| Wet temperature | Current wet bulb temperature |
| Probe temperature | Meat probe temperature (when connected) |
| Humidity | Current relative humidity |
| Timer | Cook timer remaining (seconds) |
| Mode | `idle` / `preheat` / `cook` |
| Dry / Wet / Probe temperature setpoint | Target temperatures from whatever cook is running |
| Humidity setpoint | Target humidity percentage |
| Fan speed | Current fan speed percentage |
| Rack position | Current rack position |
| Timer start type | `Immediately` / `When preheated` / etc. |
| Heating elements | `Top` / `Bottom` / `Rear` / combinations / `All` / `Off` |
| Firmware / Hardware version | Diagnostic |
| Cook session | Current cook session ID or `idle` |

### Binary Sensors (4, read-only)

| Entity | Description |
|---|---|
| Connectivity | Online / offline |
| Door | Open / closed |
| Water tank | Empty (problem) + `descale_required` attribute |
| Vent | Open / closed |

### Climate (1)

`OFF` and `HEAT` HVAC modes, target temperature 25-250 C (25-100 C in wet mode), and `dry` / `wet` presets on v2 ovens. See [Climate Entity Behavior](#climate-entity-behavior).

### Switch (1)

Lamp on/off.

### Number (1, v2 ovens with the app login)

**Cook timer** (minutes): sets the timer on the running cook without restarting it (`CMD_APO_SET_TIMER`). Available only while cooking.

## Climate Entity Behavior

The climate entity is intentionally simple.

**Oven idle:** `turn_on` starts a new single-stage cook with no timer, using the selected preset:

| Parameter | `dry` preset | `wet` preset (sous vide) |
|---|---|---|
| Bulb | Dry | Wet |
| Steam (relative humidity) | 0% | 100% |
| Heating elements | Top + Rear | Top + Rear |
| Fan speed | 100% | 100% |
| Vent | Closed | Closed |

**Oven cooking (v2 ovens, with the app login):** changing the target temperature (same preset) changes the setpoint of the running cook in place (`CMD_APO_SET_TEMPERATURE_BULBS`): same cook, timer and stages.

**Oven cooking (v2 ovens, otherwise):** changing the preset, or the temperature without the app login, resends the running cook from its active stage with only the bulb mode and setpoint changed. Steam, fan, heating elements, rack position and later stages are kept, and a running stage timer continues with the time it had left. Anova's personal-token API has no "adjust current cook" command, so the cook gets a new cook ID.

**Oven cooking (v1 ovens):** a temperature change replaces the running cook with the simple cook above.

`turn_off` sends `CMD_APO_STOP`.

## Anova App Login (optional, for the lamp and camera)

Anova refuses the lamp command (`unauthorized`) when it is sent with a Personal Access Token. Verified on an oven_v2: with the login, `CMD_APO_SET_LAMP` turns the oven light on and off, and the oven reports it as `doorLamp`. To control the lamp, enter your Anova app email and password when adding the integration, or later under **Configure > Anova app login**. The integration then sends lamp commands over a separate connection that authenticates the way the Anova app does. Without the login, the lamp switch behaves as before.

- The login must be an email and password. If you sign in to Anova with Google, Facebook or Apple, first set a password for the same email on the [Anova password reset page](https://anovaculinary.io/ali/password-reset), then use that password here.
- The password is not stored; only the sign-in's refresh token is kept.
- This uses the app's unofficial protocol and may stop working if Anova changes its app. Everything else in the integration is unaffected.
- To remove the login, open **Configure** and clear the email.

### Camera (v2 ovens, with the app login and the camera relay)

v2 ovens can get a **Camera** entity of the oven cavity. The oven only streams while cooking; otherwise the camera shows the last picture taken. Anova may require a subscription for the camera.

The oven streams VP8 video over WebRTC from Cloudflare, which Home Assistant's built-in go2rtc cannot receive. The camera therefore needs the **Anova Camera Relay** app (add-on) from this repository, which runs [MediaMTX](https://github.com/bluenviron/mediamtx) to re-serve the stream as RTSP inside Home Assistant:

1. **Settings > Apps > App store > ⋮ > Repositories**: add `https://github.com/rawmediaau/anova-ovens`.
2. Install and start **Anova Camera Relay**, and copy its hostname from the app's Info page.
3. In the integration, open **Configure > Oven camera** and enter the hostname.

The relay only pulls video from Anova while something reads it. Snapshots decode one frame with ffmpeg and are cheap. **Live view** (an option in the same dialog, off by default) converts the video to H264 while someone watches; on a 4-vCPU VM this added about 9 percentage points of CPU during viewing, and nothing once the viewer closed. With live view off, camera cards show refreshing snapshots.

Viewing the camera starts the oven's live stream over the app login (`CMD_APO_START_LIVE_STREAM`). The stream is kept alive while watched or requested and stopped (`CMD_APO_STOP_LIVE_STREAM`) after 10 minutes without a request or when the cook ends. The first snapshot after the stream starts can take longer than the 8 seconds Home Assistant waits, so it may fail once; it arrives in the background and later requests return it.

## Known Issues

- **First live-stream start after a restart may be refused.** Right after Home Assistant starts, Anova sometimes answers the first `CMD_APO_START_LIVE_STREAM` with `unauthorized`. The camera recovers on the next request. Likely cause: the app-login connection sends its command as soon as the socket opens, while the Anova app waits briefly first.

## Installation

### HACS

1. In HACS, open the three-dot menu and select **Custom repositories**
2. Add `https://github.com/rawmediaau/anova-ovens` as an **Integration**
3. Install "Anova Ovens" and restart HA

### Manual

Copy `custom_components/anova_ovens/` into your HA config's `custom_components/` directory and restart.

## Configuration

**Settings > Devices & Services > Add Integration > Anova Ovens**, then paste your Personal Access Token (starts with `anova-`). The same form optionally takes your Anova app email and password, which the lamp and camera need on v2 ovens.

Optional extras, from the integration's **Configure** menu:

- **Anova app login**: add, change or remove the app login later (see [Anova App Login](#anova-app-login-optional-for-the-lamp-and-camera)).
- **Oven camera**: the camera relay's hostname and the live view option (see [Camera](#camera-v2-ovens-with-the-app-login-and-the-camera-relay)).

Tokens are documented in Anova's [authentication guide](https://developer.anovaculinary.com/docs/devices/wifi/authentication). Max 10 PATs per account. WebSocket connections time out after 30 minutes of inactivity; the integration auto-reconnects.

## Repository layout

| Path | What it is |
|---|---|
| `custom_components/anova_ovens/` | The integration (installed by HACS) |
| `anova_camera_relay/` | The **Anova Camera Relay** app (add-on), only needed for the camera |
| `repository.yaml` | Makes this repository an app (add-on) repository |

## Migrating from wrongdoug/ha-anova-precision-oven

The domains differ (`anova_precision_oven` vs `anova_ovens`), so Home Assistant treats this as a new integration. Remove the original integration and its HACS entry, install this one, and add it again with your token. Entities are recreated; rename them in **Settings > Entities** if you want to keep your old entity IDs.

## License

MIT, including the original author's copyright. See `LICENSE`.
