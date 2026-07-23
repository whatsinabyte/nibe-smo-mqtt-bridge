# Security Policy

## Supported versions

Only the latest release is actively maintained and receives security fixes.

| Version | Supported |
|---|---|
| 1.0.x | ✅ Yes |

## Reporting a vulnerability

If you discover a security vulnerability in this add-on, please report it
responsibly rather than opening a public GitHub issue.

**To report a vulnerability:**

1. Open a [GitHub Security Advisory](../../security/advisories/new) in this
   repository — this keeps the details private until a fix is available
2. Describe the vulnerability, the potential impact, and steps to reproduce it
3. You will receive a response within 7 days

**Please do not:**
- Open a public issue describing a security vulnerability
- Share the details publicly before a fix has been released

## Scope

This add-on communicates with your Nibe controller over your local network using
HTTPS with credentials you provide. It does not communicate with any external
servers or cloud services.

Security considerations specific to this add-on:

- **Credentials** — your Nibe API username and password are stored in the
  Home Assistant add-on configuration. Protect access to your HA instance
  accordingly. As an alternative, credentials can be supplied as a pre-encoded
  Basic auth token in `secrets.yaml` to keep raw credentials out of the add-on
  configuration entirely. MQTT credentials (`mqtt_username` and `mqtt_password`)
  can also be supplied via `secrets.yaml` using the keys `mqtt_user` and
  `mqtt_password` — this keeps all credentials out of `/data/options.json`.
  See the [Documentation](DOCS.md) for details on the `secrets.yaml` format.
- **Local network** — the add-on requires host network access to reach your
  Nibe device and MQTT broker. It does not expose any ports or services of
  its own.
- **MQTT** — if your Mosquitto broker has authentication disabled, any device
  on your local network can publish to the broker topics used by this add-on.
  Enabling authentication on the broker is strongly recommended. When credentials
  are configured but `mqtt_tls` is not enabled, the add-on logs a warning at
  startup as passwords and command payloads travel in plaintext.
- **TLS — Nibe device API** — the Nibe controller uses a self-signed certificate
  for its local API. By default the add-on accepts this certificate without
  verification, because Nibe does not provide a way to install a trusted
  certificate on the controller. This means a device performing a man-in-the-middle
  attack on your LAN could intercept API traffic including your credentials.
  If you have a local CA that signed the controller's certificate (or can
  extract and trust it), you can supply the CA certificate path via `nibe_ca_cert`
  in the add-on options — full chain verification and hostname checking will then
  be enabled. On a trusted home network, accepting the self-signed cert is a
  common and accepted trade-off.
- **TLS — MQTT broker** — by default, MQTT traffic (including credentials and
  all entity command payloads) is sent in plaintext over your local network.
  Set `mqtt_tls: true` in the add-on options to encrypt broker traffic. If your
  broker uses a certificate signed by a private CA, also set `mqtt_ca_cert` to
  the path of that CA certificate; otherwise the system CA store is used. The
  Mosquitto add-on bundled with Home Assistant supports TLS on port 8883.
- **HA Supervisor API** — the add-on uses the HA Supervisor REST API and
  WebSocket API (via `SUPERVISOR_TOKEN`) for three purposes: sending and
  dismissing persistent notifications, subscribing to entity registry events
  for real-time enable/disable synchronisation, and provisioning the companion
  Lovelace dashboard on first start. These operations use the token provided
  automatically by the HA supervisor to all add-ons and do not require any
  additional user-granted permissions.
- **Write commands** — writable entities send values directly to the controller
  with no additional confirmation step. Anyone with access to your HA instance
  can write to any enabled writable entity. Restrict access to your HA instance
  accordingly, and be selective about which writable registers you enable.

## Acknowledgements

Responsible disclosure of security vulnerabilities is appreciated and
contributors will be credited in the release notes.