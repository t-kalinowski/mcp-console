#!/usr/bin/env -S uv run --script

from pathlib import Path

from _support import McpClient, Transcript, code, run_this_suite


def test_initializes_and_lists_tools(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    assert client.temporary_directory is not None
    workspace = Path(client.temporary_directory.name)
    client._initialize_and_list_tools()
    transcript = client._finish()
    assert not (workspace / ".mcp-console").exists(), workspace
    return transcript


def test_validates_send_arguments(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    client.send(
        # fmt: python
        python=code("""
            print("hello")
        """),
        wait_ms=0,
    )
    client.send(r="1", python="1", sql="SELECT 1")
    client.send(r=None)
    output = client.transcript[-1]["result"]["content"][0]["text"]
    assert output == "\n[idle]", output
    return client._finish()


def test_validates_session_arguments(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    client.session(action="prepare")
    result = client.transcript[-1]["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == ("`requirements` is required with `prepare`")

    client.session(action="prepare", requirements={})
    result = client.transcript[-1]["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == (
        "at least one of `requirements.r` or `requirements.python` is required"
    )

    client.session(action="prepare", requirements={"r": [""]})
    result = client.transcript[-1]["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == "R requirement strings must not be empty"

    client.session(
        action="prepare",
        requirements={"r": ["cli\ndplyr"]},
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == (
        "R requirement strings must not contain NUL or line breaks"
    )

    client.session(
        action="restart",
        requirements={"python": []},
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == (
        "`requirements.python` must contain at least one requirement"
    )

    client.session(
        action="restart",
        requirements={"r": ["cli"]},
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == (
        "`requirements.r` is not supported with `restart`"
    )
    return client._finish()


if __name__ == "__main__":
    run_this_suite(__file__)
