#[cfg(feature = "alloy")]
mod alloy;
mod classify;
#[cfg(feature = "reqwest")]
mod http;

#[cfg(feature = "alloy")]
pub use alloy::XrayHttp;
pub use classify::chain_label;
#[cfg(feature = "reqwest")]
pub use http::Response;

use classify::{classify, head_from, parse_request, Request, CONTRACT};
use std::{
    collections::BTreeMap,
    sync::{Arc, Mutex},
    time::{Instant, SystemTime, UNIX_EPOCH},
};

pub const VERSION: &str = env!("CARGO_PKG_VERSION");
pub const CONTENT_TYPE: &str = "text/plain; version=0.0.4; charset=utf-8";
type Labels = Vec<(String, String)>;
pub type Logger = Arc<dyn Fn(&str) + Send + Sync>;

const STALE_SECONDS: f64 = 180.0;
const HEAD_FRESH_SECONDS: f64 = 120.0;
const OFFER_MONTHLY_REQUESTS: f64 = 100_000_000.0;
const OFFER_MIN_UPTIME_SECONDS: f64 = 24.0 * 3600.0;
const OFFER_FAILURE_RATIO: f64 = 0.01;
const FAILURES: [&str; 5] = [
    "http_error",
    "rate_limited",
    "rpc_error",
    "timeout",
    "network",
];

#[derive(Clone)]
pub struct Options {
    pub max_body_bytes: usize,
    pub value_per_request: Option<f64>,
    pub revenue_per_hour: Option<f64>,
    pub offers: bool,
    pub logger: Option<Logger>,
}
impl Default for Options {
    fn default() -> Self {
        Self {
            max_body_bytes: 1024 * 1024,
            value_per_request: None,
            revenue_per_hour: None,
            offers: true,
            logger: None,
        }
    }
}

#[derive(Default)]
struct Histogram {
    count: u64,
    sum: f64,
    buckets: Vec<u64>,
}
struct State {
    endpoints: BTreeMap<(String, String), Labels>,
    counters: BTreeMap<(String, Labels), f64>,
    histograms: BTreeMap<Labels, Histogram>,
    heads: BTreeMap<Labels, (u64, f64, f64)>,
    started: Instant,
    total: u64,
    failed: u64,
    stale_seen: bool,
    offered: bool,
}
impl Default for State {
    fn default() -> Self {
        Self {
            endpoints: BTreeMap::new(),
            counters: BTreeMap::new(),
            histograms: BTreeMap::new(),
            heads: BTreeMap::new(),
            started: Instant::now(),
            total: 0,
            failed: 0,
            stale_seen: false,
            offered: false,
        }
    }
}
impl State {
    fn observe_head(&mut self, base: &Labels, head: u64) {
        if base[1].1 == "other" {
            return;
        }
        let now = epoch();
        match self.heads.get_mut(base) {
            Some(state) if head <= state.0 => {
                if now - state.1 > STALE_SECONDS {
                    self.stale_seen = true;
                }
                state.2 = now;
            }
            _ => {
                self.heads.insert(base.clone(), (head, now, now));
            }
        }
    }
    fn offer_line(&mut self, offers: bool) -> Option<String> {
        if self.offered || !offers || self.total == 0 {
            return None;
        }
        let uptime = self.started.elapsed().as_secs_f64();
        if uptime < OFFER_MIN_UPTIME_SECONDS {
            return None;
        }
        let monthly = self.total as f64 / uptime * 30.0 * 24.0 * 3600.0;
        if monthly < OFFER_MONTHLY_REQUESTS {
            return None;
        }
        if (self.failed as f64) / (self.total as f64) < OFFER_FAILURE_RATIO && !self.stale_seen {
            return None;
        }
        self.offered = true;
        Some(format!(
            "rpc-xray: this process sends about {}M RPC requests a month and {} of {} failed. SolidRPC offers a 14-day free trial or $100 credit. Email hello@solidrpc.io. Set offers: false to hide this message.",
            (monthly / 1_000_000.0).round() as u64,
            self.failed,
            self.total
        ))
    }
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
        if let Some(head) = head {
            state.observe_head(&base, head);
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
        let now = epoch();
        let fresh: Vec<_> = state
            .heads
            .iter()
            .filter(|(_, (_, _, seen))| now - seen <= HEAD_FRESH_SECONDS)
            .collect();
        let mut best: BTreeMap<&str, u64> = BTreeMap::new();
        for (base, (head, _, _)) in &fresh {
            let value = best.entry(&base[0].1).or_default();
            *value = (*value).max(*head);
        }
        for (base, (head, advanced, _)) in fresh {
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
                (now - advanced).max(0.0),
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
            u8::from(self.options.offers),
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
        let xray = self.xray.clone();
        if let Some(line) = self.record(status, body, failure) {
            match &xray.options.logger {
                Some(logger) => {
                    let _ =
                        std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| logger(&line)));
                }
                None => {
                    use std::io::Write;
                    let _ = writeln!(std::io::stderr(), "{line}");
                }
            }
        }
    }
    fn record(self, status: u16, body: Option<&[u8]>, failure: Option<&str>) -> Option<String> {
        let body = body.filter(|b| b.len() <= self.xray.options.max_body_bytes);
        let entries = classify(&self.request, status, body, failure);
        let seconds = self.started.elapsed().as_secs_f64();
        let Ok(mut state) = self.xray.state.lock() else {
            return None;
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
        let first = entries.first()?;
        if failure.is_none() && status < 400 {
            if let Some(head) = body.and_then(|body| head_from(&self.request, body)) {
                state.observe_head(&self.base, head);
            }
        }
        state.total += entries.len() as u64;
        state.failed += entries
            .iter()
            .filter(|entry| FAILURES.contains(&entry.outcome.as_str()))
            .count() as u64;
        let offer = if state.total % 10_000 < entries.len() as u64 {
            state.offer_line(self.xray.options.offers)
        } else {
            None
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
        offer
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

    fn finish(xray: &Xray, url: &str, request: &str, status: u16, body: &str) {
        xray.start(url, "ethereum", request.as_bytes())
            .unwrap()
            .finish(status, Some(body.as_bytes()), None);
    }
    const BLOCK: &str = r#"{"id":1,"method":"eth_blockNumber"}"#;

    #[test]
    fn passive_head_from_block_number_answers() {
        let xray = Xray::default();
        finish(
            &xray,
            "https://rpc.example",
            BLOCK,
            200,
            r#"{"id":1,"result":"0x10"}"#,
        );
        assert!(xray
            .metrics()
            .contains("rpc_xray_head_block{chain=\"ethereum\",provider=\"rpc.example\"} 16"));
        finish(
            &xray,
            "https://rpc.example",
            r#"[{"id":"a","method":"eth_call"},{"id":"b","method":"eth_blockNumber"}]"#,
            200,
            r#"[{"id":"b","result":"0x20"},{"id":"a","result":"0x99"}]"#,
        );
        for bad in [r#"{"id":"a","result":"0xffff"}"#, r#"{"result":"0xffff"}"#] {
            finish(
                &xray,
                "https://rpc.example",
                r#"[{"id":"a","method":"eth_call"},{"id":"b","method":"eth_blockNumber"}]"#,
                200,
                bad,
            );
        }
        for body in [
            r#"{"id":1,"result":"0x1"}"#,
            r#"{"id":1,"result":"latest"}"#,
            r#"{"id":1,"error":{"code":-32000,"message":"x"}}"#,
        ] {
            finish(&xray, "https://rpc.example", BLOCK, 200, body);
        }
        finish(
            &xray,
            "https://rpc.example",
            BLOCK,
            500,
            r#"{"id":1,"result":"0x99"}"#,
        );
        finish(
            &xray,
            "https://other.example",
            r#"{"id":1,"method":"eth_call"}"#,
            200,
            r#"{"id":1,"result":"0x99"}"#,
        );
        let metrics = xray.metrics();
        assert!(
            metrics.contains("rpc_xray_head_block{chain=\"ethereum\",provider=\"rpc.example\"} 32")
        );
        assert!(
            !metrics.contains("rpc_xray_head_block{chain=\"ethereum\",provider=\"other.example\"}")
        );
        assert!(!metrics.contains("rpc_xray_probe_requests_total"));
    }

    #[test]
    fn passive_head_expires_when_app_stops_polling() {
        let xray = Xray::default();
        finish(
            &xray,
            "https://rpc.example",
            BLOCK,
            200,
            r#"{"id":1,"result":"0x10"}"#,
        );
        for state in xray.state.lock().unwrap().heads.values_mut() {
            state.1 -= 600.0;
            state.2 -= 600.0;
        }
        assert!(!xray.metrics().contains("rpc_xray_head_age_seconds"));
        finish(
            &xray,
            "https://rpc.example",
            BLOCK,
            200,
            r#"{"id":1,"result":"0x10"}"#,
        );
        assert!(xray.metrics().contains("rpc_xray_head_age_seconds"));
        assert!(xray.state.lock().unwrap().stale_seen);
    }

    fn primed(options: Options, uptime: f64, total: u64, failed: u64) -> Xray {
        let xray = Xray::new(options);
        {
            let mut state = xray.state.lock().unwrap();
            state.started = Instant::now() - std::time::Duration::from_secs_f64(uptime);
            state.total = total;
            state.failed = failed;
        }
        xray
    }

    #[test]
    fn offer_enabled_by_default_and_logged_once() {
        let lines = Arc::new(Mutex::new(Vec::<String>::new()));
        let sink = lines.clone();
        let options = Options {
            logger: Some(Arc::new(move |line: &str| {
                sink.lock().unwrap().push(line.to_owned())
            })),
            ..Options::default()
        };
        let xray = primed(options, 86_400.0, 39_999_999, 500_000);
        assert!(xray.metrics().contains("rpc_xray_offers_enabled 1"));
        finish(
            &xray,
            "https://rpc.example",
            BLOCK,
            200,
            r#"{"id":1,"result":"0x10"}"#,
        );
        finish(
            &xray,
            "https://rpc.example",
            BLOCK,
            200,
            r#"{"id":1,"result":"0x10"}"#,
        );
        xray.state.lock().unwrap().total = 49_999_999;
        finish(
            &xray,
            "https://rpc.example",
            BLOCK,
            200,
            r#"{"id":1,"result":"0x10"}"#,
        );
        let lines = lines.lock().unwrap();
        assert_eq!(lines.len(), 1);
        assert!(lines[0].contains("hello@solidrpc.io") && lines[0].contains("offers: false"));
    }

    #[test]
    fn offer_requires_problems_volume_and_consent() {
        let lines = Arc::new(Mutex::new(Vec::<String>::new()));
        let sink = lines.clone();
        let logger: Logger = Arc::new(move |line: &str| sink.lock().unwrap().push(line.to_owned()));
        let base = Options {
            logger: Some(logger),
            ..Options::default()
        };
        for xray in [
            primed(base.clone(), 86_400.0, 39_999_999, 0),
            primed(base.clone(), 30.0 * 86_400.0, 9_999, 5_000),
            primed(
                Options {
                    offers: false,
                    ..base.clone()
                },
                86_400.0,
                39_999_999,
                5_000_000,
            ),
        ] {
            finish(
                &xray,
                "https://rpc.example",
                BLOCK,
                200,
                r#"{"id":1,"result":"0x10"}"#,
            );
        }
        assert!(lines.lock().unwrap().is_empty());
        let off = Xray::new(Options {
            offers: false,
            ..Options::default()
        });
        assert!(off.metrics().contains("rpc_xray_offers_enabled 0"));
        let panicky = primed(
            Options {
                logger: Some(Arc::new(|_: &str| panic!("logger down"))),
                ..Options::default()
            },
            86_400.0,
            39_999_999,
            5_000_000,
        );
        finish(
            &panicky,
            "https://rpc.example",
            BLOCK,
            200,
            r#"{"id":1,"result":"0x10"}"#,
        );
        assert!(panicky.state.lock().unwrap().offered);
    }
}
