use serde::Serialize;
use serde_json::Value;
use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::Duration;
use tauri::{LogicalSize, Manager, Size};

#[derive(Serialize)]
struct BridgeConfig {
    mode: String,
}

fn bridge_credentials() -> Result<(String, String), String> {
    let url = std::env::var("KAYDEN_BRIDGE_URL")
        .map_err(|_| "KAYDEN_BRIDGE_URL is missing".to_string())?;
    let token = std::env::var("KAYDEN_BRIDGE_TOKEN")
        .map_err(|_| "KAYDEN_BRIDGE_TOKEN is missing".to_string())?;
    Ok((url, token))
}

fn bridge_request(
    method: &str,
    path: &str,
    body: Option<Value>,
    last_event: Option<u64>,
) -> Result<Value, String> {
    let (url, token) = bridge_credentials()?;
    let address = url
        .strip_prefix("http://")
        .filter(|value| !value.contains('/'))
        .ok_or_else(|| "Kayden bridge URL must be loopback HTTP".to_string())?;
    if !address.starts_with("127.0.0.1:") {
        return Err("Kayden bridge must be bound to 127.0.0.1".to_string());
    }

    let encoded_body = match body {
        Some(value) => serde_json::to_string(&value)
            .map_err(|error| format!("Could not encode Kayden command: {error}"))?,
        None => String::new(),
    };
    let mut request = format!(
        "{method} {path} HTTP/1.1\r\nHost: {address}\r\nAuthorization: Bearer {token}\r\nConnection: close\r\n"
    );
    if let Some(event_id) = last_event {
        request.push_str(&format!("X-Kayden-Last-Event: {event_id}\r\n"));
    }
    if method == "POST" {
        request.push_str("Content-Type: application/json\r\n");
        request.push_str(&format!("Content-Length: {}\r\n", encoded_body.len()));
    }
    request.push_str("\r\n");
    request.push_str(&encoded_body);

    let mut stream = TcpStream::connect(address)
        .map_err(|error| format!("Could not reach Kayden bridge: {error}"))?;
    let timeout = Some(Duration::from_secs(20));
    stream
        .set_read_timeout(timeout)
        .map_err(|error| format!("Could not configure Kayden bridge: {error}"))?;
    stream
        .set_write_timeout(timeout)
        .map_err(|error| format!("Could not configure Kayden bridge: {error}"))?;
    stream
        .write_all(request.as_bytes())
        .map_err(|error| format!("Could not send to Kayden bridge: {error}"))?;

    let mut response = Vec::new();
    stream
        .read_to_end(&mut response)
        .map_err(|error| format!("Could not read Kayden bridge response: {error}"))?;
    let separator = response
        .windows(4)
        .position(|window| window == b"\r\n\r\n")
        .ok_or_else(|| "Kayden bridge returned malformed HTTP".to_string())?;
    let headers = String::from_utf8_lossy(&response[..separator]);
    let status = headers
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .and_then(|code| code.parse::<u16>().ok())
        .ok_or_else(|| "Kayden bridge returned an invalid status".to_string())?;
    let response_body = &response[separator + 4..];
    if !(200..300).contains(&status) {
        return Err(format!(
            "Kayden bridge returned {status}: {}",
            String::from_utf8_lossy(response_body)
        ));
    }
    serde_json::from_slice(response_body)
        .map_err(|error| format!("Kayden bridge returned invalid JSON: {error}"))
}

async fn bridge_request_async(
    method: &'static str,
    path: String,
    body: Option<Value>,
    last_event: Option<u64>,
) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        bridge_request(method, &path, body, last_event)
    })
    .await
    .map_err(|error| format!("Kayden bridge task failed: {error}"))?
}

#[tauri::command]
fn bridge_config() -> Result<BridgeConfig, String> {
    Ok(BridgeConfig {
        mode: std::env::var("KAYDEN_WINDOW_MODE").unwrap_or_else(|_| "gui".to_string()),
    })
}

#[tauri::command]
async fn bridge_frontend_config() -> Result<Value, String> {
    bridge_request_async("GET", "/config".to_string(), None, None).await
}

#[tauri::command]
async fn bridge_events(last_event: u64) -> Result<Value, String> {
    bridge_request_async("GET", "/events".to_string(), None, Some(last_event)).await
}

#[tauri::command]
async fn bridge_command(path: String, body: Value) -> Result<Value, String> {
    const ALLOWED_PATHS: &[&str] = &[
        "/commands/submit",
        "/commands/interrupt",
        "/commands/model",
        "/commands/capture/start",
        "/commands/capture/finish",
        "/commands/capture/cancel",
        "/commands/close",
    ];
    if !ALLOWED_PATHS.contains(&path.as_str()) {
        return Err("Unsupported Kayden bridge command".to_string());
    }
    bridge_request_async("POST", path, Some(body), None).await
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            let window = app
                .get_webview_window("main")
                .ok_or("main window was not created")?;
            let assistant_name = std::env::var("KAYDEN_ASSISTANT_NAME")
                .unwrap_or_else(|_| "Kayden".to_string());
            if std::env::var("KAYDEN_WINDOW_MODE").as_deref() == Ok("floaty") {
                window.set_title(&format!("{assistant_name} · Floaty"))?;
                window.set_always_on_top(true)?;
                window.set_size(Size::Logical(LogicalSize::new(560.0, 440.0)))?;
                window.set_min_size(Some(Size::Logical(LogicalSize::new(380.0, 280.0))))?;
            } else {
                window.set_title(&assistant_name)?;
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            bridge_config,
            bridge_frontend_config,
            bridge_events,
            bridge_command,
        ])
        .run(tauri::generate_context!())
        .expect("error while running Kayden desktop");
}
