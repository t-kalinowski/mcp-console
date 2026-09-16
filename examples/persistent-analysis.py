"""Run from a writable workspace with the installed mcp-console[client] extra."""

import os
import sys
from pathlib import Path
from textwrap import dedent

import anyio
from mcp_console import AsyncMCPConsole


async def send_cell(console: AsyncMCPConsole, **cell: str) -> str:
    """Collect a cell before submitting the next one; a wait timeout is not completion."""
    running = "\n[running; poll with an empty send]"
    chunks = []
    response = await console.send(**cell, timeout_ms=10_000)
    while response.endswith(running):
        chunks.append(response.removesuffix(running))
        response = await console.send(timeout_ms=10_000)
    if response != "[done]":
        chunks.append(response)
    output = "".join(chunks)
    print(output, end="" if output.endswith("\n") else "\n", flush=True)
    return output


async def main() -> None:
    sessions = Path.cwd() / ".agents/console/sessions"
    previous = set(sessions.glob("*"))
    # An optional executable path follows the other examples' test convention.
    command = sys.argv[1] if len(sys.argv) > 1 else None
    # This walkthrough prepares its packages in managed Python.
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    with anyio.fail_after(600):
        async with AsyncMCPConsole(
            command=command, server_parameters={"env": environment}
        ) as console:
            await send_cell(
                console,
                # fmt: r
                r=dedent(r"""
                    orders <- data.frame(
                      channel = rep(c("web", "store"), each = 3),
                      revenue = c(120, 150, 180, 100, 140, 160),
                      cost = c(80, 90, 120, 70, 100, 110)
                    )
                    orders$profit <- orders$revenue - orders$cost
                    total_profit <- sum(orders$profit)
                    cat("Total profit:", total_profit, "\n")
                    """),
            )
            await send_cell(
                console,
                sql=dedent("""
                    SELECT channel, SUM(profit) AS profit
                    FROM orders
                    GROUP BY channel
                    ORDER BY channel
                    """),
            )
            await send_cell(
                console,
                # fmt: python
                python=dedent("""
                    import matplotlib.pyplot as plt

                    profits = r.orders.groupby("channel", sort=True)["profit"].sum()
                    profits.plot.bar(rot=0, color=["steelblue", "darkorange"])
                    plt.ylabel("Profit ($)")
                    plt.title("Profit from six synthetic orders")
                    plt.tight_layout()
                    print(f"Best channel: {profits.idxmax()} (${profits.max():.0f} profit)")
                    """),
            )
            # Reuse the previous cell's Python object without recreating the data.
            follow_up = await send_cell(
                console,
                # fmt: python
                python=dedent("""
                    print(f"Profit gap: ${profits['web'] - profits['store']:.0f}")
                    """),
            )
            assert "Profit gap: $40" in follow_up, follow_up

    (session,) = set(sessions.glob("*")) - previous
    (plot,) = (session / "artifacts").glob("*.png")
    transcript = session / "transcript.md"
    assert transcript.is_file()
    print("Session closed.")
    print(f"Transcript: {transcript}")
    print(f"Plot: {plot}")


if __name__ == "__main__":
    anyio.run(main)
