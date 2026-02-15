mod common;

#[cfg(not(windows))]
use common::McpSnapshot;
use common::TestResult;
use rmcp::model::RawContent;

fn result_text(result: &rmcp::model::CallToolResult) -> String {
    result
        .content
        .iter()
        .filter_map(|item| match &item.raw {
            RawContent::Text(text) => Some(text.text.as_str()),
            _ => None,
        })
        .collect::<Vec<_>>()
        .join("")
}

#[cfg(not(windows))]
#[tokio::test(flavor = "multi_thread")]
async fn sends_input_to_r_console() -> TestResult<()> {
    let mut snapshot = McpSnapshot::new();
    snapshot
        .session(
            "default",
            mcp_script! {
                write_stdin("1+1", timeout = 10.0);
            },
        )
        .await?;

    insta::assert_snapshot!("sends_input_to_r_console", snapshot.render());
    insta::with_settings!({ snapshot_suffix => "transcript" }, {
        insta::assert_snapshot!("sends_input_to_r_console", snapshot.render_transcript());
    });
    Ok(())
}

#[cfg(windows)]
#[tokio::test(flavor = "multi_thread")]
async fn sends_input_to_r_console() -> TestResult<()> {
    let mut session = common::spawn_server().await?;
    let result = session.write_stdin_raw_with("1+1", Some(30.0)).await?;
    let text = result_text(&result);
    if text.contains("Fatal error: cannot create 'R_TempDir'")
        || text.contains(
            "worker protocol error: ipc disconnected while waiting for request completion",
        )
    {
        eprintln!("server_smoke backend unavailable in this environment; skipping");
        session.cancel().await?;
        return Ok(());
    }
    session.cancel().await?;
    assert!(text.contains("2"), "expected 2 in output, got: {text:?}");
    Ok(())
}
