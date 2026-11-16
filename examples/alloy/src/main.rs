use alloy::{
    providers::{Provider, ProviderBuilder},
    rpc::client::ClientBuilder,
};
use axum::{extract::State, routing::get, Router};
use serde_json::{json, Value};
use solidrpc_rpc_xray::{Xray, XrayHttp, CONTENT_TYPE};
use std::{sync::Arc, time::Duration};
use tokio::sync::{watch, RwLock};

#[derive(Clone)]
struct App {
    telemetry: Xray,
    state: Arc<RwLock<Value>>,
}
async fn metrics(
    State(app): State<App>,
) -> ([(axum::http::header::HeaderName, &'static str); 1], String) {
    (
        [(axum::http::header::CONTENT_TYPE, CONTENT_TYPE)],
        app.telemetry.metrics(),
    )
}
async fn state(State(app): State<App>) -> axum::Json<Value> {
    axum::Json(app.state.read().await.clone())
}

async fn run() -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let rpc_url = std::env::var("RPC_URL")?;
    let chain = std::env::var("CHAIN")?;
    let url = reqwest::Url::parse(&rpc_url)?;
    if !matches!(url.scheme(), "http" | "https") || chain.trim().is_empty() {
        return Err("set RPC_URL and CHAIN".into());
    }
    let host = std::env::var("HOST").unwrap_or_else(|_| "127.0.0.1".into());
    let port = std::env::var("PORT")
        .unwrap_or_else(|_| "9464".into())
        .parse::<u16>()?;
    let listener = tokio::net::TcpListener::bind((host.as_str(), port)).await?;
    let address = listener.local_addr()?;
    let http_client = reqwest::Client::builder()
        .timeout(Duration::from_millis(4500))
        .redirect(reqwest::redirect::Policy::none())
        .retry(reqwest::retry::never())
        .build()?;
    let telemetry = Xray::default();
    let transport = XrayHttp::new(http_client, url, telemetry.clone(), chain.clone());
    let is_local = transport.guess_local();
    let client = ClientBuilder::default().transport(transport, is_local);
    let provider = ProviderBuilder::new().connect_client(client);
    let app = App {
        telemetry,
        state: Arc::new(RwLock::new(
            json!({"chain":chain,"status":"starting","blockNumber":null}),
        )),
    };
    let router = Router::new()
        .route("/metrics", get(metrics))
        .route("/", get(state))
        .with_state(app.clone());
    let (stop_tx, mut stop_rx) = watch::channel(false);
    let poller = tokio::spawn(async move {
        let mut next_poll = tokio::time::Instant::now();
        loop {
            tokio::select! { biased; _=stop_rx.changed()=>break, _=tokio::time::sleep_until(next_poll)=>{} }
            next_poll = tokio::time::Instant::now() + Duration::from_secs(1);
            let result = tokio::select! { biased; _=stop_rx.changed()=>break, result=provider.get_block_number()=>result };
            let mut state = app.state.write().await;
            match result {
                Ok(block) => {
                    state["status"] = json!("ok");
                    state["blockNumber"] = json!(format!("0x{block:x}"));
                }
                Err(_) => state["status"] = json!("error"),
            }
        }
    });
    println!("RPC example listening on {address}. Metrics at /metrics. Press Ctrl+C to stop.");
    let shutdown_tx = stop_tx.clone();
    let shutdown = async move {
        #[cfg(unix)]
        {
            let mut term =
                tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
                    .expect("signal handler");
            tokio::select! { _=tokio::signal::ctrl_c()=>{}, _=term.recv()=>{} }
        }
        #[cfg(not(unix))]
        {
            let _ = tokio::signal::ctrl_c().await;
        }
        let _ = shutdown_tx.send(true);
    };
    let result = axum::serve(listener, router)
        .with_graceful_shutdown(shutdown)
        .await;
    let _ = stop_tx.send(true);
    let _ = poller.await;
    result?;
    Ok(())
}

#[tokio::main]
async fn main() {
    if run().await.is_err() {
        eprintln!("Could not start. Check RPC_URL, CHAIN, HOST, and PORT.");
        std::process::exit(1);
    }
}
