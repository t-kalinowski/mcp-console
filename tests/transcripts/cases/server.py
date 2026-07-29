from textwrap import dedent

from _runner import run_this_case
from support import McpClient


server_invocations = {
    "mcp-console": (),
    "mcp-console serve": ("serve",),
}


def run(client: McpClient) -> None:
    client.initialize_and_list_tools()
    client.console(
        # fmt: r
        python=dedent("""
            print('hello')
        """).strip(),
        wait_ms=0,
    )


if __name__ == "__main__":
    run_this_case(__file__)
