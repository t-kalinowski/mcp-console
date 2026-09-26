//! Python-only MCP capability projection. R-present schemas remain unchanged.

use serde_json::{Map, Value};

pub(super) fn configure(
    description: &mut String,
    properties: &mut Map<String, Value>,
    python_preparation: bool,
) {
    let remaining = description
        .split_once("\n\nSend one complete")
        .expect("shared send description")
        .1;
    let environment = if python_preparation {
        "Console manages Python through uv. Explicit requirements.python can prepare packages before the first worker starts or with control: restart. Changed requirements on a running worker require an explicit restart; already retained requirements are a no-op. Restart resets objects and retains the accepted environment."
    } else {
        "Python uses the environment selected at server startup; restart resets objects and retains that environment."
    };
    *description = format!(
        "Persistent local Python workbench. State persists across calls. R and SQL cells, live requirements, and automatic package installation are unavailable in this session. {environment}\n\nSend one complete{remaining}"
    );
    *description = description.replace("`r`, `python`, or `sql` cell", "`python` cell");
    for (field, description) in [
        (
            "python",
            "One complete Python cell in persistent state. The final expression displays automatically. Use input() for managed stdin; Matplotlib plots return as PNG images when installed. Use only packages already in the selected environment. R integration, SQL cells, live requirements, and automatic package installation are unavailable. Use control: restart with python to run in a fresh worker using the same environment, or timeout_ms: 0 then poll for background execution.",
        ),
        (
            "control",
            "Applies lifecycle control alone or before compatible same-call fields. interrupt requests SIGINT from the live worker and preserves Python state. After successful delivery, stdin is queued and send waits 100 milliseconds before observing the earlier evaluation or attempting an optional following cell; that cell is not run if the interrupted evaluation remains active. restart discards Python objects, debugger state, and unread stdin, retains the selected environment, and sends same-call stdin and code only to the replacement worker.",
        ),
        (
            "stdin",
            r"Input for an active read, prompt, or debugger. When responding to active input, omit code and send stdin on its own. Its UTF-8 encoding is queued exactly; no newline is added. Line-oriented input normally needs a trailing `\n`. After interrupt, nonempty stdin is queued before the 100-millisecond grace and may be consumed while the earlier operation unwinds. After restart, same-call stdin goes only to the replacement. When sent with a cell, nonempty text is queued before code runs. Empty text queues nothing. If output ends in [waiting for stdin], send the requested input here. Unread text can satisfy later reads and is discarded by restart.",
        ),
        (
            "timeout_ms",
            "Omit for normal calls and polls. Defaults to 60,000 milliseconds. This limits the wait after cell dispatch or attachment to an active evaluation and includes one automatic worker replacement attempt. Reaching the timeout does not cancel execution or startup. Inline control, interrupt grace, and restart happen before dispatch and may make the complete call take longer. Use 0 for background execution, then poll with an empty send. If a response ends with [running; poll with an empty send] or [worker starting], poll without resubmitting the cell.",
        ),
    ] {
        if let Some(property) = properties.get_mut(field) {
            property["description"] = description.into();
        }
    }
    if python_preparation {
        for (field, description) in [
            (
                "python",
                "One complete Python cell in persistent state. The final expression displays automatically. Use input() for managed stdin; Matplotlib plots return as PNG images when installed. requirements.python prepares packages before first use, or with control: restart before the replacement runs this cell. Live package updates, automatic package installation, R integration, and SQL are unavailable. Use timeout_ms: 0 then poll for background execution.",
            ),
            (
                "control",
                "Applies lifecycle control alone or before compatible same-call fields. interrupt requests SIGINT from the live worker and preserves Python state; during preparation it retires the candidate sandbox. Same-call stdin is queued before the interrupt grace. Python requirements with interrupt are rejected before signaling or queuing input. restart discards objects and unread stdin, then sends same-call stdin and code only to the replacement. With requirements.python, the complete candidate environment is resolved and inspected before stopping the current worker. Preparation failure preserves the current worker, retained requirements, and queued input; same-call code and stdin are not sent. Retirement or replacement failure follows the ordinary restart contract. Plain restart reuses the accepted environment.",
            ),
        ] {
            if let Some(property) = properties.get_mut(field) {
                property["description"] = description.into();
            }
        }
        let requirements = properties
            .get_mut("requirements")
            .expect("requirements schema");
        requirements["description"] = "Explicit Python package requirements for a Console-managed uv environment. Supported before the first worker starts, alone or with a Python cell, and with control: restart, with or without code. Additions are merged with defaults and retained requirements. A changed live environment requires restart; retained requirements are a no-op. Resolution and native inspection finish before retirement. Automatic installation is unavailable. Standalone preparation cannot queue stdin.".into();
        let fields = requirements["properties"]
            .as_object_mut()
            .expect("requirement properties");
        fields.shift_remove("r");
        fields.shift_remove("duckdb");
        fields.get_mut("python").expect("Python requirements")["description"] = "Named PEP 508 registry requirements added to the retained Python manifest. Local paths, URLs, editable requirements, and archives are rejected. Preparation uses uv in a separate native sandbox and Console-owned storage. Only compatible registry wheels are supported; source builds are unavailable. Live changes require control: restart.".into();
    } else {
        properties.shift_remove("requirements");
    }
}
