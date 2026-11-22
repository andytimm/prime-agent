//! Headless e2e for the `/model` picker's name column: it grows with the
//! terminal and truncates with the ellipsis only when the row cannot fit the
//! full name beside the effort cluster and the provider.
#![cfg(unix)]
#![warn(clippy::allow_attributes_without_reason)]

mod support;

use std::os::unix::net::UnixListener;
use std::path::PathBuf;

use pa_tui::interactive::{HeadlessPlan, HeadlessStep, InteractiveOptions};
use pa_types::ai::Model;
use serde_json::json;

use support::{
    attach_response, create_response, detach_response, run_headless, serve_connection, write_json,
    Request,
};

const LONG_NAME: &str = "DeepSeek V4.1 Flash (Capella Preview, Extended Context)";

fn reasoning_model(id: &str, name: &str) -> Model {
    serde_json::from_value(json!({
        "id": id, "name": name, "api": "openai-completions", "provider": "openrouter",
        "baseUrl": "https://example.invalid/v1", "reasoning": true,
        "input": ["text"],
        "cost": { "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0 },
        "contextWindow": 128_000, "maxTokens": 4096,
    }))
    .expect("mock model deserializes")
}

fn catalog() -> Vec<Model> {
    vec![
        reasoning_model("deepseek/deepseek-v4.1-flash", LONG_NAME),
        reasoning_model("z-ai/glm-5.3", "GLM 5.3"),
    ]
}

fn serve(listener: &UnixListener) {
    let (stream, _) = listener.accept().expect("accept");
    serve_connection(stream, &[], |writer, Request { id, kind, .. }| {
        match kind {
            "create" => write_json(writer, &create_response(id)),
            "attach" => write_json(writer, &attach_response(id, "picker session")),
            "get_model_catalog" => write_json(
                writer,
                &json!({
                    "type": "response",
                    "id": id,
                    "command": "get_model_catalog",
                    "success": true,
                    "data": { "models": catalog(), "configuredProviders": ["openrouter"] },
                }),
            ),
            "detach" => write_json(writer, &detach_response(id)),
            _ => return false,
        }
        true
    });
}

fn options(socket: PathBuf) -> InteractiveOptions {
    InteractiveOptions {
        model_catalog: catalog(),
        model_configured_providers: std::iter::once("openrouter".to_string()).collect(),
        ..support::options(socket)
    }
}

/// The picker's row for the long-named model at `width` columns.
fn long_name_row(width: u16) -> String {
    let outcome = run_headless(
        HeadlessPlan {
            steps: vec![
                HeadlessStep::Submit("/model".to_string()),
                HeadlessStep::WaitRender {
                    needle: "GLM 5.3".to_string(),
                    timeout_ms: 30_000,
                },
            ],
            width,
            height: 30,
        },
        options,
        |socket| {
            let listener = UnixListener::bind(socket).expect("bind mock socket");
            move || serve(&listener)
        },
    );
    let last = outcome.frames.last().expect("a frame with the picker open");
    last.split('\n')
        .find(|row| row.contains("DeepSeek V4.1"))
        .expect("the long-named row renders")
        .to_string()
}

#[test]
fn a_narrow_terminal_truncates_the_long_name_with_an_ellipsis() {
    let row = long_name_row(60);
    assert!(
        row.contains("DeepSeek V4.1 Flash (Capella\u{2026}") && !row.contains(LONG_NAME),
        "the name truncates to fit the effort and provider columns: {row}"
    );
    assert!(row.contains("openrouter"), "the provider stays: {row}");
}

#[test]
fn a_wide_terminal_shows_the_long_name_in_full() {
    let row = long_name_row(120);
    assert!(
        row.contains(LONG_NAME) && !row.contains('\u{2026}'),
        "the name column grows into the spare width: {row}"
    );
    assert!(row.contains("openrouter"), "the provider stays: {row}");
}
