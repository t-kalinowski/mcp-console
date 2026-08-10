#!/usr/bin/env -S uv run --script

from pathlib import Path

from _support import McpClient, Transcript, code, run_this_suite


def test_initializes_and_lists_tools(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client.initialize_and_list_tools()
    return client.finish()


def test_validates_send_arguments(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client.initialize_and_list_tools()
    client.call_tool(
        "send",
        # fmt: python
        python=code("""
            print("hello")
        """),
        wait_ms=0,
    )
    client.call_tool("send", r="1", python="1", sql="SELECT 1")
    client.call_tool("send", r=None)
    output = client.transcript[-1]["result"]["content"][0]["text"]
    assert output == "\n[idle]", output
    return client.finish()


def test_validates_session_arguments(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client.initialize_and_list_tools()
    client.call_tool("session", action="prepare")
    result = client.transcript[-1]["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == ("`requirements` is required with `prepare`")

    client.call_tool("session", action="prepare", requirements={})
    result = client.transcript[-1]["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == (
        "at least one of `requirements.r` or `requirements.python` is required"
    )

    client.call_tool("session", action="prepare", requirements={"r": [""]})
    result = client.transcript[-1]["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == "R requirement strings must not be empty"

    client.call_tool(
        "session",
        action="prepare",
        requirements={"r": ["cli\ndplyr"]},
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == (
        "R requirement strings must not contain NUL or line breaks"
    )

    client.call_tool(
        "session",
        action="restart",
        requirements={"python": ["py-yaml12"]},
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == (
        "`requirements` is not yet supported with `restart`"
    )
    return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
