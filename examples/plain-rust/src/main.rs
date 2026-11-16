use axum::{extract::State, routing::get, Router};
use serde_json::{json, Value};
use solidrpc_rpc_xray::{Xray, CONTENT_TYPE};
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
    let client = reqwest::Client::builder()
        .timeout(Duration::from_millis(4500))
        .build()?;
    let app = App {
        telemetry: Xray::default(),
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
        let mut ticker = tokio::time::interval(Duration::from_secs(1));
        ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        let mut id = 0;
        loop {
            tokio::select! { biased; _=stop_rx.changed()=>break, _=ticker.tick()=>{} }
            id += 1;
            let call = async {
                let request = client
                    .post(url.clone())
                    .json(&json!({"jsonrpc":"2.0","id":id,"method":"eth_blockNumber","params":[]}))
                    .build()?;
                let response = app.telemetry.execute(&client, request, &chain).await?;
                let status = response.status();
                let payload: Value = response.json().await?;
                let result = payload["result"]
                    .as_str()
                    .filter(|s| s.starts_with("0x") && u64::from_str_radix(&s[2..], 16).is_ok());
                if !status.is_success() || !payload["error"].is_null() || result.is_none() {
                    return Err("invalid RPC result".into());
                }
                Ok::<_, Box<dyn std::error::Error + Send + Sync>>(result.unwrap().to_owned())
            };
            let result = tokio::select! { biased; _=stop_rx.changed()=>break, result=call=>result };
            let mut state = app.state.write().await;
            match result {
                Ok(block) => {
                    state["status"] = json!("ok");
                    state["blockNumber"] = json!(block);
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
