from textwrap import dedent

from support import McpClient


server_argument_sets = ((), ("serve",))


def run(client: McpClient) -> None:
    client.initialize_and_list_tools()
    client.console(
        # fmt: r
        python=dedent("""
            print('hello')
        """).strip(),
        wait_ms=0,
    )
