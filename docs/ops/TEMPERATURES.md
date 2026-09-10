# Dashboard temperatures

The Operate page shows current CPU and GPU temperatures for the Mac, Corsair
and Red Linux. Storage, memory and other sensors are under each host's
**Other sensors** disclosure. GPU edge, junction and memory readings retain
their own labels and device identifiers. Values are Celsius; high/critical
labels use only thresholds supplied by the hardware sensor.

The existing authenticated devices endpoint supplies `temperature_hosts`.
The Mac samples off the API event loop with a ten-second cache. Corsair uses
the existing restricted actuator status request; Red uses its existing status
observer. Reading temperatures never invokes a model start or wake operation.
Samples expire after 45 seconds on both the server and dashboard. Missing,
invalid or expired readings are unavailable, never zero. Ridge has no collector
yet and is shown as unavailable without probing or waking it.

Mac prerequisite: `brew install macmon` (installed version 0.8.2). One bounded,
unprivileged `macmon pipe -s 1 -i 100` invocation reads sensor averages; there is
no new daemon. Single-digit Apple sensor averages observed on this machine
are rejected as implausible. A missing GPU reading does not hide a valid CPU
reading. See [macmon](https://github.com/vladkens/macmon).

Linux reads the kernel's [hwmon interface](https://kernel.org/doc/html/v5.16/hwmon/sysfs-interface.html)
and NVIDIA's read-only temperature query. Faulted/disabled sensors are omitted.
The canonical stdlib collector is `api/aria/infrastructure/thermals.py`.
Deploy it beside Corsair's installed `corsair_actuator.py` and copy the same
file beside Red's `/opt/red-r9700/status.py` **before** deploying callers that
import it. The maintained Red artifact is also mirrored in
`CorsairModelHost/red-r9700/thermals.py`. No model restart is needed.

Dashboard: https://bens-macbook-pro.tailb286a5.ts.net/operate (Tailscale required).
