# M Maps Android Companion

This is the Android target-side companion for M Maps. The macOS controller sends coordinates over
an ADB-forwarded localhost socket, and this app publishes them through Android's standard fused
mock-location API. It has no telemetry and opens no network-facing socket.

## Build and setup

1. Enable Developer Options and USB debugging on the Android phone.
2. Open this directory in Android Studio and run the `app` configuration on the phone.
3. Approve the location permission and select **M Maps Companion** under Developer options →
   Mock location app.
4. From the repository root, run:

```sh
sudo venv/bin/python3 m_maps.py serve
```

The normal server automatically detects iPhones and Android phones together and attaches the only
connected phone. If both are connected with no active session, it asks you to unplug one instead of
guessing. It maps Mac port `8766` to device-local port `8765`, starts the
companion when necessary, and opens the normal map UI. The legacy
`venv/bin/python3 m_maps.py serve --android` command remains available for Android-only testing
without sudo.

## Transport contract

The companion accepts newline-delimited JSON commands and replies `{"ok":true}` to each accepted
command. The acknowledgment is required-an ADB forwarding socket can otherwise accept a Mac-side
connection even when the phone service is dead.

```json
{"action":"SET_LOCATION","latitude":48.8584,"longitude":2.2945}
```

```json
{"action":"CLEAR_LOCATION"}
```

`PING` checks service readiness. `SET_LOCATION` accepts optional `altitude`, `speed`, `bearing`,
and `accuracy`. The foreground service refreshes the latest target every three seconds, so Android
does not discard it as stale and the hold continues after USB disconnect. `CLEAR_LOCATION` removes
the target and disables fused mock mode.

## Verified device

The current prototype is manually verified on a Samsung Galaxy A15 (SM-A156U), Android 16:
Teleport, road movement, Fly, five-minute hold, USB disconnect/reconnect, automatic service
restart, and Restore GPS. Other OEMs still need testing.
