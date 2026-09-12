#!/usr/bin/env -S uv run --script

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.server.test_ssh_lease import fault
from support.assertions import last_result_text, wait_for_evaluation_output
from support.client import McpClient
from support.requirements import requires
from support.resolvers import send_and_collect_runtime_python_resolution
from support.ssh import CONFIG, EXTERNAL_SSH
from support.suites import run_this_suite


@requires(EXTERNAL_SSH)
def test_cross_host_recovery_preserves_r_and_python_state(binary):
    external = json.loads(os.environ["MCP_CONSOLE_TEST_SSH_EXTERNAL"])
    with TemporaryDirectory() as temporary:
        local = Path(temporary)
        proxy = Path(__file__).resolve().parents[3] / "fixtures/ssh_tcp_proxy.py"
        wrapper = local / "ssh-wrapper.py"
        wrapper.write_text(
            "import os, shlex, sys\n"
            + f"config = {external['ssh_config']!r}\nproxy = {str(proxy)!r}\nroot = {str(local)!r}\n"
            + "role = shlex.split(sys.argv[-1])[-1]\n"
            + "command = shlex.join([sys.executable, proxy, root, role]) + ' %h %p'\n"
            + "os.execv('/usr/bin/ssh', ['/usr/bin/ssh', '-F', config, '-o', 'ControlPath=none', '-o', 'ProxyCommand=' + command, *sys.argv[1:]])\n"
        )
        ssh = local / "ssh"
        ssh.write_text(
            "#!/bin/sh\nexec " + shlex.join([sys.executable, str(wrapper)]) + ' "$@"\n'
        )
        ssh.chmod(0o755)
        config = local / CONFIG
        config.parent.mkdir(parents=True)
        config.write_text(
            json.dumps(
                {
                    "extends": ":workspace",
                    "target": {**external["target"], "lease_ms": 12000},
                    "sandbox": {"environment": external["environment"]},
                }
            )
        )
        environment = {
            **os.environ,
            "PATH": str(local),
            "R_HOME": "/controller-must-not-discover-R",
        }
        with McpClient(
            binary, ("serve",), environment, local, response_timeout=180
        ) as client:
            client.initialize_and_list_tools()
            send_and_collect_runtime_python_resolution(
                client,
                r="identity <- Sys.getpid(); r_value <- 41L; cat(Sys.getenv('TMPDIR'))",
            )
            private = last_result_text(client)
            assert private.startswith("/"), private
            client.send(python="import os; identity = os.getpid(); python_value = 42")
            fault(local, "ssh-launch", "up")
            wait_for_evaluation_output(
                client,
                "[1] 42\n",
                "cross-host input recovery",
                completion_timeout_seconds=14,
                r="stopifnot(Sys.getpid() == identity); r_value <- r_value + 1L; r_value",
                timeout_ms=0,
            )
            fault(local, "ssh-launch", "down")
            wait_for_evaluation_output(
                client,
                "42\n",
                "cross-host output recovery",
                completion_timeout_seconds=14,
                python="assert os.getpid() == identity; print(python_value)",
                timeout_ms=0,
            )
            client.finish()
        subprocess.run(
            [
                "/usr/bin/ssh",
                "-F",
                external["ssh_config"],
                "--",
                external["target"]["transport"]["host"],
                "test ! -e " + shlex.quote(private),
            ],
            check=True,
            timeout=15,
        )
        return [
            {
                "input_blackhole_recovered": True,
                "output_blackhole_recovered": True,
                "same_remote_worker": True,
                "r_state": 42,
                "python_state": 42,
                "confirmed_sandbox_retirement": True,
            }
        ]


if __name__ == "__main__":
    run_this_suite(__file__)
