#[cfg(feature = "alloy")]
mod alloy;
mod classify;
#[cfg(feature = "reqwest")]
mod http;

#[cfg(feature = "alloy")]
pub use alloy::{XrayLayer, XrayService};
pub use classify::chain_label;
#[cfg(feature = "reqwest")]
pub use http::Response;

use classify::{classify, parse_request, Request, CONTRACT};
use std::{
    collections::BTreeMap,
    sync::{Arc, Mutex},
    time::{Instant, SystemTime, UNIX_EPOCH},
};

pub const VERSION: &str = env!("CARGO_PKG_VERSION");
pub const CONTENT_TYPE: &str = "text/plain; version=0.0.4; charset=utf-8";
type Labels = Vec<(String, String)>;

#[derive(Clone)]
pub struct Options {
    pub max_body_bytes: usize,
    pub value_per_request: Option<f64>,
    pub revenue_per_hour: Option<f64>,
}
impl Default for Options {
    fn default() -> Self {
        Self {
            max_body_bytes: 1024 * 1024,
            value_per_request: None,
            revenue_per_hour: None,
        }
    }
}

#[derive(Default)]
struct Histogram {
    count: u64,
    sum: f64,
    buckets: Vec<u64>,
}
#[derive(Default)]
struct State {
    endpoints: BTreeMap<(String, String), Labels>,
    counters: BTreeMap<(String, Labels), f64>,
    histograms: BTreeMap<Labels, Histogram>,
    heads: BTreeMap<Labels, (u64, f64)>,
}
impl State {
    fn endpoint(&mut self, raw: &str, chain: &str) -> Option<Labels> {
        let key = (raw.to_owned(), chain.to_owned());
        if let Some(labels) = self.endpoints.get(&key) {
            return Some(labels.clone());
        }
        if self.endpoints.len() >= 32 {
            return Some(vec![
                ("chain".into(), "unknown".into()),
                ("provider".into(), "other".into()),
            ]);
        }
        let url = url::Url::parse(raw).ok()?;
        if !matches!(url.scheme(), "http" | "https") {
            return None;
        }
        let mut host = url.host_str()?.to_lowercase();
        if let Some(port) = url.port() {
            host.push_str(&format!(":{port}"));
        }
        let labels = vec![
            (
                "chain".into(),
                if chain.is_empty() { "unknown" } else { chain }.into(),
            ),
            ("provider".into(), host),
        ];
        self.endpoints.insert(key, labels.clone());
        Some(labels)
    }
    fn inc(&mut self, name: &str, labels: Labels, value: f64) {
        let key = (name.to_owned(), labels);
        if self.counters.contains_key(&key) || self.counters.len() + self.histograms.len() < 5000 {
            *self.counters.entry(key).or_default() += value;
        }
    }
}

#[derive(Clone)]
pub struct Xray {
    state: Arc<Mutex<State>>,
    options: Options,
}
impl Default for Xray {
    fn default() -> Self {
        Self::new(Options::default())
    }
}

impl Xray {
    pub fn new(mut options: Options) -> Self {
        if options.max_body_bytes == 0 {
            options.max_body_bytes = 1024 * 1024;
        }
        Self {
            state: Arc::new(Mutex::new(State::default())),
            options,
        }
    }
    pub fn start(&self, url: &str, chain: &str, body: &[u8]) -> Option<Observation> {
        if body.len() > self.options.max_body_bytes {
            return None;
        }
        let request = parse_request(body)?;
        let base = self.state.lock().ok()?.endpoint(url, chain)?;
        Some(Observation {
            xray: self.clone(),
            request,
            base,
            started: Instant::now(),
        })
    }
    pub fn record_probe(&self, url: &str, chain: &str, head: Option<u64>) {
        let Ok(mut state) = self.state.lock() else {
            return;
        };
        let Some(base) = state.endpoint(url, chain) else {
            return;
        };
        state.inc(
            "rpc_xray_probe_requests_total",
            with(&base, "result", if head.is_some() { "ok" } else { "error" }),
            1.0,
        );
        if let Some(head) = head.filter(|_| base[1].1 != "other") {
            if state.heads.get(&base).is_none_or(|(old, _)| head > *old) {
                state.heads.insert(base, (head, epoch()));
            }
        }
    }
    pub fn metrics(&self) -> String {
        let Ok(state) = self.state.lock() else {
            return String::new();
        };
        let mut families: BTreeMap<String, (&str, Vec<String>)> = BTreeMap::new();
        fn sample(
            families: &mut BTreeMap<String, (&str, Vec<String>)>,
            name: &str,
            kind: &'static str,
            labels: &Labels,
            value: impl std::fmt::Display,
        ) {
            families
                .entry(name.to_owned())
                .or_insert((kind, Vec::new()))
                .1
                .push(format!("{name}{} {value}", render_labels(labels)));
        }
        for ((name, labels), value) in &state.counters {
            sample(&mut families, name, "counter", labels, value);
        }
        for (labels, h) in &state.histograms {
            let name = "rpc_xray_request_duration_seconds";
            let lines = &mut families
                .entry(name.into())
                .or_insert(("histogram", Vec::new()))
                .1;
            for (bound, count) in CONTRACT.buckets.iter().zip(&h.buckets) {
                lines.push(format!(
                    "{name}_bucket{} {count}",
                    render_labels(&with(labels, "le", &bound.to_string()))
                ));
            }
            lines.push(format!(
                "{name}_bucket{} {}",
                render_labels(&with(labels, "le", "+Inf")),
                h.count
            ));
            lines.push(format!("{name}_sum{} {}", render_labels(labels), h.sum));
            lines.push(format!("{name}_count{} {}", render_labels(labels), h.count));
        }
        let mut best: BTreeMap<&str, u64> = BTreeMap::new();
        for (base, (head, _)) in &state.heads {
            let value = best.entry(&base[0].1).or_default();
            *value = (*value).max(*head);
        }
        for (base, (head, advanced)) in &state.heads {
            sample(&mut families, "rpc_xray_head_block", "gauge", base, head);
            sample(
                &mut families,
                "rpc_xray_head_last_advance_timestamp_seconds",
                "gauge",
                base,
                advanced,
            );
            sample(
                &mut families,
                "rpc_xray_head_age_seconds",
                "gauge",
                base,
                (epoch() - advanced).max(0.0),
            );
            sample(
                &mut families,
                "rpc_xray_head_lag_blocks",
                "gauge",
                base,
                best[base[0].1.as_str()] - head,
            );
        }
        for (name, value) in [
            (
                "rpc_xray_value_per_request_usd",
                self.options.value_per_request,
            ),
            (
                "rpc_xray_revenue_per_hour_usd",
                self.options.revenue_per_hour,
            ),
        ] {
            if let Some(value) = value.filter(|v| v.is_finite() && *v >= 0.0) {
                sample(&mut families, name, "gauge", &Vec::new(), value);
            }
        }
        sample(
            &mut families,
            "rpc_xray_offers_enabled",
            "gauge",
            &Vec::new(),
            0,
        );
        sample(
            &mut families,
            "rpc_xray_info",
            "gauge",
            &vec![
                ("version".into(), VERSION.into()),
                ("lang".into(), "rust".into()),
            ],
            1,
        );
        let mut text = String::new();
        for (name, (kind, lines)) in families {
            text.push_str(&format!(
                "# HELP {name} RPC X-Ray {name}.\n# TYPE {name} {kind}\n"
            ));
            for line in lines {
                text.push_str(&line);
                text.push('\n');
            }
        }
        text
    }
}

pub struct Observation {
    xray: Xray,
    request: Request,
    base: Labels,
    started: Instant,
}
#[cfg(any(feature = "reqwest", feature = "alloy"))]
struct Pending(Option<Observation>);
#[cfg(any(feature = "reqwest", feature = "alloy"))]
impl Drop for Pending {
    fn drop(&mut self) {
        if let Some(observation) = self.0.take() {
            observation.finish(0, None, Some("timeout"));
        }
    }
}
impl Observation {
    pub fn finish(self, status: u16, body: Option<&[u8]>, failure: Option<&str>) {
        let body = body.filter(|b| b.len() <= self.xray.options.max_body_bytes);
        let entries = classify(&self.request, status, body, failure);
        let seconds = self.started.elapsed().as_secs_f64();
        let Ok(mut state) = self.xray.state.lock() else {
            return;
        };
        for entry in &entries {
            state.inc(
                "rpc_xray_requests_total",
                with(
                    &with(&self.base, "method", &entry.method),
                    "outcome",
                    &entry.outcome,
                ),
                1.0,
            );
        }
        let Some(first) = entries.first() else {
            return;
        };
        let labels = with(
            &self.base,
            "method",
            if self.request.batch {
                "batch"
            } else {
                &first.method
            },
        );
        if state.histograms.contains_key(&labels)
            || state.histograms.len() + state.counters.len() < 5000
        {
            let h = state.histograms.entry(labels).or_insert_with(|| Histogram {
                buckets: vec![0; CONTRACT.buckets.len()],
                ..Histogram::default()
            });
            h.count += 1;
            h.sum += seconds;
            for (bound, count) in CONTRACT.buckets.iter().zip(&mut h.buckets) {
                if seconds <= *bound {
                    *count += 1;
                }
            }
        }
        if seconds > 1.0 {
            state.inc("rpc_xray_slow_seconds_total", self.base, seconds - 1.0);
        }
    }
}
fn epoch() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|v| v.as_secs_f64())
        .unwrap_or_default()
}
fn with(labels: &Labels, key: &str, value: &str) -> Labels {
    let mut result = labels.clone();
    result.push((key.into(), value.into()));
    result
}
fn render_labels(labels: &Labels) -> String {
    if labels.is_empty() {
        return String::new();
    }
    let pairs: Vec<_> = labels
        .iter()
        .map(|(key, value)| {
            format!(
                "{key}=\"{}\"",
                value
                    .replace('\\', "\\\\")
                    .replace('"', "\\\"")
                    .replace('\n', "\\n")
            )
        })
        .collect();
    format!("{{{}}}", pairs.join(","))
}

#[cfg(test)]
mod metric_tests {
    use super::*;
    #[test]
    fn metric_contract() {
        let xray = Xray::new(Options {
            value_per_request: Some(0.1),
            revenue_per_hour: Some(100.0),
            ..Options::default()
        });
        let mut observation = xray
            .start(
                "https://rpc.example",
                "ethereum",
                br#"{"method":"eth_blockNumber","id":1}"#,
            )
            .unwrap();
        observation.started = Instant::now() - std::time::Duration::from_millis(1500);
        observation.finish(200, Some(br#"{"id":1,"result":"0x10"}"#), None);
        xray.record_probe("https://rpc.example", "ethereum", Some(10));
        let metrics = xray.metrics();
        assert_eq!(metrics.matches("# TYPE ").count(), 12);
        if let Ok(path) = std::env::var("RPC_XRAY_METRICS_FIXTURE") {
            std::fs::write(path, metrics).unwrap();
        }
    }
}
