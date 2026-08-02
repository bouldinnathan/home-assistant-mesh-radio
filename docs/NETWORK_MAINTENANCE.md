# Automatic Network Maintenance

MeshNet can optionally collect fresher Meshtastic NeighborInfo metadata during
quiet periods. This feature is **off by default**. It is deliberately narrower
than a general network scan: it selects one exact node at a time, sends only a
NeighborInfo request, and never runs traceroute automatically.

NeighborInfo is the target radio's cached zero-hop neighbor report. It is not a
live route survey, and an empty response is different from a timeout. Firmware
support is experimental; the request path has been verified with Meshtastic
2.7.26 and requires the target's Neighbor Info module to be enabled.

## Enable or Disable It

Use the Home Assistant integration options; there is no YAML switch, service,
or WebSocket command that starts an automatic cycle:

1. Open **Settings → Devices & services → MeshNet**.
2. Select **Configure**.
3. Choose **Automatic network maintenance**.
4. Select **Enable automatic low-traffic maintenance**.
5. Select one exact configured Meshtastic Bluetooth gateway.
6. Review the interval, quiet time, and maximum requests, then save.

Only a direct, local Meshtastic Bluetooth gateway is eligible. Serial, TCP,
MQTT, REST, MeshCore, Bluetooth proxies, an automatic gateway choice, and a
name-based gateway match are not accepted. If no eligible gateway exists, the
form cannot enable maintenance. Saving options reloads the config entry and
starts a new scheduler lifecycle.

To stop future automatic requests, return to the same **Configure** action,
clear the enable switch, and save. Manual administrator controls remain
available when automatic maintenance is off.

## Timing and Airtime Limits

| Control | Default | Allowed range or invariant |
| --- | ---: | --- |
| Enabled | Off | Explicit opt-in only |
| Cycle interval | 3,600 seconds | 3,600–86,400 seconds |
| Required quiet time | 120 seconds | 60–3,600 seconds |
| Maximum requests per cycle | 10 | 1–60 |
| Idle evaluation tick | 15 seconds | Fixed; at most one candidate per tick |
| Shared metadata airtime floor | 60 seconds | Fixed integration-wide minimum |
| NeighborInfo same-target floor | 180 seconds | Fixed per-target minimum |

The first cycle is due only after one full configured interval. With the
default/minimum setting, that means a full hour after the scheduler is created
or resumed—saving the option does not submit a request. Reload creates a fresh
scheduler, while an ordinary gateway reconnect never creates catch-up work. If
a cycle was already due, it still must pass the quiet, busy, and cooldown gates.
The next interval starts after the current cycle completes.

MeshNet evaluates at most one candidate and invokes at most one request per
scheduler tick. Successful and failed request attempts remain at least 60
seconds apart. The durable 60-second metadata floor is shared by manual
traceroute, manual NeighborInfo, and automatic NeighborInfo across every
gateway in the MeshNet config entry. NeighborInfo also enforces a durable
180-second floor for the same exact target. Changing the gateway cannot bypass
either applicable floor.

The maximum is a cycle budget, not a promise to send that many requests. A
cycle ends early when there are no eligible targets, and cooldown, validation,
disconnect, firmware, or traffic outcomes can reduce its RF work.

## What Makes Maintenance Wait

Legitimate work always has priority. MeshNet restarts the configured quiet
window when it observes ordinary inbound packets or node updates and when a
local send, refresh, manual traceroute, or manual NeighborInfo operation begins.
It also defers while any of these owners is active:

- gateway startup or reconnect;
- an outbox flush or foreground message send;
- local gateway settings reads, previews, or writes;
- remote administration reads, previews, or writes;
- manual traceroute or NeighborInfo;
- another Bluetooth operation;
- config-entry reload, shutdown, or a radio-operation fence.

Only a response already correlated to MeshNet's own maintenance request, and
the resulting `maintenance_scan` node projection, are excluded from this
foreground-activity signal. Unsolicited or merely similar NeighborInfo traffic
does restart the quiet window.

Traffic can defer a due cycle indefinitely. MeshNet does not reserve a time
slot, force a request through a busy radio, or raise the priority after repeated
deferrals. A continuous quiet window and an idle operation boundary are both
required. The scheduler checks those gates before candidate selection and
again immediately before the RF callback. The provider checks once more before
submission.

If foreground activity wins after a candidate was selected, that candidate is
excluded for the rest of the active cycle. If activity wins after a durable
airtime reservation but before provider submission, MeshNet conservatively
keeps the reservation even though no RF was sent. Neither case is retried.

These gates cover traffic and operations MeshNet can observe. They cannot prove
that an unheard transmitter elsewhere in the mesh is silent, so operators must
still choose a conservative interval and cycle budget for their deployment.

## Exact Target Selection and Rotation

An automatic candidate must be all of the following at selection time:

- an exact, canonical, unambiguous Meshtastic node identity;
- online and observed during the current gateway session;
- observed through the selected connected Bluetooth gateway;
- local RF evidence rather than an MQTT-only observation;
- a remote node, not the selected gateway itself.

Cached-only, stale-session, ambiguous/colliding, malformed, other-gateway,
MQTT-only, self, offline, and non-Meshtastic records are rejected. MeshNet does
not broadcast a discovery request and does not guess from a name, short name,
MAC fragment, location, or old database record.

Within those constraints, nodes with no retained NeighborInfo attempt are
chosen before previously attempted nodes. Older attempts rotate before newer
ones, with stable last-heard and node-key tie breakers. Once selected, a target
is excluded for the rest of that cycle even after a timeout, failure, busy
race, cooldown rejection, or other zero-RF rejection. This prevents an unlucky
or incompatible node from consuming a cycle through repeated retries.

## No Catch-up, Retry, or Automatic Traceroute

Missing one or many intervals while Home Assistant is stopped, the gateway is
offline, or traffic is busy never creates a burst. When operation resumes,
MeshNet schedules one future cycle after a full interval or continues one
already-due cycle at its normal one-request-at-a-time pace.

MeshNet makes no automatic retry after a request failure, timeout, unknown
outcome, late traffic race, or rejected reservation. Protocol routing or radio
firmware may still relay or retransmit one submitted reliable packet; the limit
describes MeshNet submissions, not every physical RF emission.

Automatic traceroute is not supported. Traceroute remains an explicit,
administrator-only panel/WebSocket operation with its durable cooldown. The
manual NeighborInfo **Load status → Request → Confirm** control also remains
available and uses the same validated request and cooldown boundary as
maintenance.

## Results and Provenance

A correlated automatic response is stored with `maintenance_scan` provenance.
Manual responses use `manual_request`; unsolicited or legacy evidence uses
`passive`. The cached-evidence graph labels maintenance NeighborInfo separately
and does not turn physical distance or a shared gateway observation into a
node-to-node edge.

An automatic timeout does not erase older evidence or create a false empty
neighbor list. The status and graph remain cached evidence, not proof of a
currently usable route.

## Status and Privacy-safe Diagnostics

Open the main **MeshNet → Mesh** view and find the **NeighborInfo request** card
to see the automatic-maintenance summary and a link back to **Configure**. The
panel reports whether the feature is enabled, the fixed task/outcome state,
time until the next cycle, success/failure counters, and aggregate traffic/busy
deferrals.

Downloaded diagnostics include the validated configuration state and the
scheduler's fixed aggregate booleans, durations, task/outcome values, and
counters. They do not include the selected gateway ID, candidate/target IDs,
node names, addresses, raw packets, response contents, or provider exception
text. Collecting diagnostics does not connect, scan, refresh, reserve airtime,
or transmit.

The RF request and response are not private merely because diagnostics are
redacted. A target and relays can observe protocol metadata, and the returned
neighbor evidence is retained locally in `meshnet.sqlite3`. Protect Home
Assistant backups and inspect Home Assistant's outer diagnostic wrapper and
filename before sharing a report.

## Reload, Shutdown, and Uninstall

The config entry owns one recurring scheduler task. Reload and unload first
fence new radio work, then cancel and drain that task together with the other
radio-operation owners. Resume is allowed only after the previous owner has
drained and always re-arms a full interval; an old task cannot run alongside a
new one.

For the clearest no-RF uninstall sequence, disable automatic maintenance in
**Configure** and save before removing the integration or HACS package. Removing
the config entry or unloading Home Assistant also stops the scheduler. Follow
Home Assistant's restart instruction after a HACS uninstall; removing files
cannot execute cleanup code in an already running Python process.

Maintenance changes no radio settings and does not create or delete a Bluetooth
bond. Therefore uninstall has no radio configuration to roll back. Durable
cooldown rows, sanitized NeighborInfo results, and attempt history remain in
`meshnet.sqlite3`; HACS and `uninstall.sh` intentionally do not delete that
database, Home Assistant Recorder history, automations, or backups. Remove
retained data manually only after making any required backup.
