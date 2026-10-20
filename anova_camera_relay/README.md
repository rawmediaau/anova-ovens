# Anova Camera Relay

The Anova Precision Oven (v2) camera streams VP8 video over WebRTC from
Cloudflare. Home Assistant's built-in go2rtc cannot receive VP8 over WebRTC,
so this app runs [MediaMTX](https://github.com/bluenviron/mediamtx) to pull
the stream and re-serve it as RTSP for the integration.

## Setup

1. Install and start this app. It has no settings.
2. Copy its hostname from the app's Info page (for example
   `abcd1234-anova-camera-relay`).
3. In the Anova Ovens integration, open **Configure → Oven camera**
   and enter the hostname. The integration also needs the Anova app login.

## Resource use

MediaMTX only connects to Anova while something reads the stream, and does
no transcoding. Snapshots decode a single frame. The optional live view in
the integration converts VP8 to H264 with ffmpeg while someone watches, which
uses more CPU.

## Network

No ports are published on the host. The RTSP (8554) and API (9997) ports are
only reachable from Home Assistant and other apps on the internal Supervisor
network.
