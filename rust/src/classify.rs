use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{collections::BTreeMap, sync::LazyLock};

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct Contract {
    pub methods: Vec<String>,
    pub chains: BTreeMap<String, String>,
    pub buckets: Vec<f64>,
    pub caller_codes: Vec<f64>,
    pub rate_patterns: Vec<String>,
    pub caller_patterns: Vec<String>,
}

pub(crate) static CONTRACT: LazyLock<Contract> = LazyLock::new(|| {
    serde_json::from_str(include_str!("contract.json")).expect("bundled contract must be valid")
});

pub fn chain_label(id: u64) -> String {
    CONTRACT
        .chains
        .get(&id.to_string())
        .cloned()
        .unwrap_or_else(|| format!("eip155:{id}"))
}

#[derive(Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Entry {
    pub method: String,
    pub outcome: String,
}

#[derive(Clone)]
pub(crate) struct Request {
    pub batch: bool,
    pub calls: Vec<(Value, String)>,
}

pub(crate) fn parse_request(body: &[u8]) -> Option<Request> {
    let value: Value = serde_json::from_slice(body).ok()?;
    let batch = value.is_array();
    let items = if let Value::Array(items) = value {
        items
    } else {
        vec![value]
    };
    if items.is_empty() {
        return None;
    }
    let mut calls = Vec::new();
    for item in items {
        let method = item.as_object()?.get("method")?.as_str()?;
        let method = if CONTRACT.methods.iter().any(|known| known == method) {
            method
        } else {
            "other"
        };
        calls.push((
            item.get("id").cloned().unwrap_or(Value::Null),
            method.to_owned(),
        ));
    }
    Some(Request { batch, calls })
}

fn rpc_error(error: &Value) -> &'static str {
    let code = error.get("code").and_then(Value::as_f64);
    let message = error
        .get("message")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_lowercase();
    if code == Some(429.0) || CONTRACT.rate_patterns.iter().any(|p| message.contains(p)) {
        return "rate_limited";
    }
    if code.is_some_and(|c| CONTRACT.caller_codes.contains(&c))
        || CONTRACT.caller_patterns.iter().any(|p| message.contains(p))
    {
        return "caller_error";
    }
    "rpc_error"
}

fn outcome(value: &Value) -> &'static str {
    if !value.is_object() {
        return "rpc_error";
    }
    if let Some(error) = value.get("error").filter(|e| !e.is_null()) {
        return rpc_error(error);
    }
    if value.get("result").is_some() {
        "ok"
    } else {
        "rpc_error"
    }
}

fn id_key(value: &Value) -> String {
    if let Value::Number(number) = value {
        if number.is_f64() {
            if let Some(number) = number.as_f64() {
                if number.fract() == 0.0 {
                    return format!("{number:.0}");
                }
            }
        }
    }
    value.to_string()
}

pub(crate) fn classify(
    request: &Request,
    status: u16,
    body: Option<&[u8]>,
    failure: Option<&str>,
) -> Vec<Entry> {
    let all = |outcome: &str| {
        request
            .calls
            .iter()
            .map(|(_, method)| Entry {
                method: method.clone(),
                outcome: outcome.to_owned(),
            })
            .collect()
    };
    if let Some(failure) = failure {
        return all(failure);
    }
    if status == 429 {
        return all("rate_limited");
    }
    if status >= 400 {
        return all("http_error");
    }
    let Some(body) = body else {
        return all("ok");
    };
    let Ok(payload) = serde_json::from_slice::<Value>(body) else {
        return all("rpc_error");
    };
    let Value::Array(items) = payload else {
        return all(outcome(&payload));
    };
    let by_id: BTreeMap<String, &str> = items
        .iter()
        .filter(|item| item.is_object())
        .map(|item| {
            (
                id_key(item.get("id").unwrap_or(&Value::Null)),
                outcome(item),
            )
        })
        .collect();
    request
        .calls
        .iter()
        .map(|(id, method)| Entry {
            method: method.clone(),
            outcome: by_id
                .get(&id_key(id))
                .copied()
                .unwrap_or("rpc_error")
                .to_owned(),
        })
        .collect()
}

fn hex_head(value: Option<&Value>) -> Option<u64> {
    let text = value?.as_str()?;
    let digits = text.strip_prefix("0x")?;
    if digits.is_empty() || digits.len() > 16 {
        return None;
    }
    u64::from_str_radix(digits, 16).ok()
}

pub(crate) fn head_from(request: &Request, body: &[u8]) -> Option<u64> {
    let ids: Vec<String> = request
        .calls
        .iter()
        .filter(|(_, method)| method == "eth_blockNumber")
        .map(|(id, _)| id_key(id))
        .collect();
    if ids.is_empty() {
        return None;
    }
    let payload: Value = serde_json::from_slice(body).ok()?;
    let items = match payload {
        Value::Array(items) => items,
        other => vec![other],
    };
    items
        .iter()
        .filter(|item| item.is_object() && item.get("error").is_none_or(Value::is_null))
        .filter(|item| {
            item.get("id")
                .is_some_and(|id| !id.is_null() && ids.contains(&id_key(id)))
        })
        .filter_map(|item| hex_head(item.get("result")))
        .max()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn shared_vectors() {
        let vectors: Vec<Value> = serde_json::from_str(include_str!("vectors.json")).unwrap();
        for vector in vectors {
            let expected: Vec<Entry> = serde_json::from_value(vector["expected"].clone()).unwrap();
            let entries = parse_request(vector["request"].as_str().unwrap().as_bytes())
                .map(|request| {
                    classify(
                        &request,
                        vector["status"].as_u64().unwrap_or(0) as u16,
                        vector["response"].as_str().map(str::as_bytes),
                        vector["failure"].as_str(),
                    )
                })
                .unwrap_or_default();
            assert_eq!(entries, expected, "{}", vector["name"]);
        }
    }
}
