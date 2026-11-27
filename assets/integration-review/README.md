# Grafana integration captures

These captures use the provisioned Grafana dashboard and real SDK metrics from local, simulated RPC endpoints. The incident rules kept their shipped thresholds and hold times.

| View | What to look for |
|---|---|
| [No request samples](no-samples.png) | The history shows **No samples** and the incident counts remain unmeasured. |
| [Requests arriving before rule data](awaiting-rules.png) | The exporter is sending data. **Awaiting rules** distinguishes a deliberately missing rule group from an observed quiet period. |
| [Firing incident on desktop](../dashboard.png) | A Node.js app observes provider errors inside HTTP 200 responses. The current count, history, and error breakdown are visible together. |
| [Firing incident on a phone](mobile-firing.png) | Both incident counts fit in the first 390-pixel-wide viewport. |
| [History on a phone](mobile-history.png) | The Rust app's HTTP failures appear under **HttpErrors**. The single error bar retains its `http_error` label. |
| [Recovered incident](recovered.png) | The firing count returns to zero. The timeline retains the firing interval and the exporter retains its cumulative error count. |
