mod lyrics;
mod lyrics_search;
mod api {
    pub mod models;
}
use serde_json::{Value, json};
use std::io::{self, Read};
fn normalize(value: &mut Value) {
    match value {
        Value::Array(values) => {
            for v in values {
                normalize(v)
            }
        }
        Value::Object(map) => {
            if !map.contains_key("id")
                && let Some(id) = map
                    .get("urn")
                    .and_then(Value::as_str)
                    .and_then(|s| s.rsplit(':').next())
                    .and_then(|s| s.parse::<u64>().ok())
            {
                map.insert("id".into(), json!(id));
            }
            for v in map.values_mut() {
                normalize(v)
            }
        }
        _ => {}
    }
}
async fn run(input: Value) -> Result<Value, String> {
    if input["action"] == "lookup" {
        return lyrics::lookup(
            input["artist"].as_str().unwrap_or_default().into(),
            input["title"].as_str().unwrap_or_default().into(),
            input["durationMs"].as_u64().unwrap_or_default(),
            input["album"].as_str().map(str::to_owned),
            input["isrc"].as_str().map(str::to_owned),
        )
        .await
        .map(|v| json!(v));
    }
    let query = input["query"].as_str().unwrap_or_default();
    let matches = lyrics_search::search(query).await?;
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(15))
        .no_proxy()
        .build()
        .map_err(|_| "Could not start track lookup")?;
    let base =
        std::env::var("FASTCLOUD_INTERNAL_URL").unwrap_or_else(|_| "http://127.0.0.1:8080".into());
    let token = input["token"].as_str().ok_or("Sign in required")?;
    let mut results = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for found in matches.into_iter().take(6) {
        let response = client
            .get(format!("{base}/v1/soundcloud/api/tracks"))
            .header("Authorization", format!("OAuth {token}"))
            .query(&[
                (
                    "q",
                    format!("{} {}", found.record.artist_name, found.record.track_name),
                ),
                ("limit", "25".into()),
                ("access", "playable,preview".into()),
            ])
            .send()
            .await
            .map_err(|_| "Track search unavailable")?;
        if !response.status().is_success() {
            continue;
        }
        let mut data: Value = response
            .json()
            .await
            .map_err(|_| "Invalid track search response")?;
        normalize(&mut data);
        let Some(tracks) = data["collection"].as_array() else {
            continue;
        };
        for value in tracks {
            if let Ok(track) = serde_json::from_value::<api::models::Track>(value.clone()) {
                let score = lyrics_search::track_score(&found, &track);
                if score > 0 && seen.insert(track.id) {
                    results.push(json!({"track":value,"excerpt":found.excerpt,"matchScore":found.score,"recordingScore":score}));
                }
            }
        }
    }
    results.sort_by_key(|v| {
        std::cmp::Reverse((
            v["matchScore"].as_u64().unwrap_or(0),
            v["recordingScore"].as_u64().unwrap_or(0),
        ))
    });
    results.truncate(30);
    Ok(json!(results))
}
#[tokio::main]
async fn main() {
    let mut input = String::new();
    if io::stdin().take(32769).read_to_string(&mut input).is_err() || input.len() > 32768 {
        println!("{{\"error\":\"Invalid request\"}}");
        return;
    }
    let response = match serde_json::from_str(&input) {
        Ok(value) => {
            match tokio::time::timeout(std::time::Duration::from_secs(80), run(value)).await {
                Ok(Ok(value)) => json!({"data":value}),
                _ => json!({"error":"Lyrics service temporarily unavailable"}),
            }
        }
        Err(_) => json!({"error":"Invalid request"}),
    };
    println!("{response}");
}
