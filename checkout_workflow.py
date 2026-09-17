"""Shared ownership and run records for commands that mutate a source checkout."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

LOCKS_ENV = "MCP_CONSOLE_CHECKOUT_LOCKS"
RUN_ENV = "MCP_CONSOLE_VALIDATION_RUN"
GROUP_ENV = "MCP_CONSOLE_VALIDATION_GROUP"
FAILURE = re.compile(
    r"^((?:client_server|server_relay|relay_worker|cli)/\S+::\S+): failed(?: in .*)?$"
)


@contextmanager
def exclusive(paths: list[Path], label: str) -> Iterator[None]:
    """Refuse conflicting owners; sequential descendants inherit the active token."""
    inherited = os.environ.get(LOCKS_ENV, "{}")
    tokens = json.loads(inherited)
    owner = ""
    # A nested owner must find its inherited slot before claiming a free one.
    for path in sorted(paths, key=lambda path: str(path) not in tokens):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock.seek(0)
                owner = lock.read()
                if (
                    str(path) in tokens
                    and json.loads(owner)["token"] == tokens[str(path)]
                ):
                    yield
                    return
                continue
            token = uuid.uuid4().hex
            lock.seek(0)
            lock.truncate()
            json.dump({"pid": os.getpid(), "command": sys.argv, "token": token}, lock)
            lock.flush()
            os.environ[LOCKS_ENV] = json.dumps(tokens | {str(path): token})
            try:
                yield
            finally:
                os.environ[LOCKS_ENV] = inherited
            return
    raise SystemExit(
        f"{label} is busy; retry after its owner finishes. Lock: {path}\nOwner: {owner}"
    )


@contextmanager
def checkout_owner(root: Path) -> Iterator[None]:
    # Installation tests rename target, so ownership must live outside it.
    with exclusive([root.resolve() / ".dev-workflow/checkout.lock"], "checkout"):
        yield


@contextmanager
def full_check_slot() -> Iterator[None]:
    slots = int(os.environ.get("MCP_CONSOLE_CHECK_SLOTS", "1"))
    if slots < 1:
        raise SystemExit("MCP_CONSOLE_CHECK_SLOTS must be at least 1")
    cache = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    paths = [
        cache.resolve() / "mcp-console/checks" / f"slot-{index}.lock"
        for index in range(slots)
    ]
    with exclusive(paths, "full-check budget"):
        yield


def stop_phase(process: subprocess.Popen, *, owns_group: bool) -> None:
    def deliver(number: int) -> None:
        try:
            if owns_group:
                os.killpg(process.pid, number)
            else:
                os.kill(process.pid, number)
        except ProcessLookupError:
            pass

    deadline = time.monotonic() + 5
    deliver(signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    if owns_group:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        # Group membership has no portable wait primitive once the leader exits.
        # Give remaining members the rest of the grace period without polling.
        remaining = deadline - time.monotonic()
        if remaining > 0:
            print(
                "[cleanup] waiting for phase descendants", file=sys.stderr, flush=True
            )
            time.sleep(remaining)
        deliver(signal.SIGKILL)
    elif process.poll() is None:
        deliver(signal.SIGKILL)
    process.wait()


class Run:
    def __init__(self, root: Path, command: list[str]) -> None:
        runs = root / ".dev-workflow/runs"
        runs.mkdir(parents=True, exist_ok=True)
        self.directory = Path(
            tempfile.mkdtemp(prefix=time.strftime("%Y%m%d-%H%M%S-"), dir=runs)
        )
        self.path = self.directory / "result.json"
        self.started = time.monotonic()
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True
        )
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True
        )
        self.record = {
            "checkout": str(root),
            "command": command,
            "revision": revision.stdout.strip() if revision.returncode == 0 else None,
            "worktree_status": status.stdout if status.returncode == 0 else None,
            "parent_record": os.environ.get(RUN_ENV),
            "phases": [],
            "failing_selectors": [],
            "exit_status": None,
        }
        self.save()

    def save(self) -> None:
        self.record["elapsed_seconds"] = time.monotonic() - self.started
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.record, indent=2) + "\n")
        temporary.replace(self.path)

    def phase(self, name: str, command: list[str]) -> int:
        log_path = self.directory / f"{len(self.record['phases']) + 1:02}-{name}.log"
        started = time.monotonic()
        status = 1
        owns_group = os.environ.get(GROUP_ENV) != str(os.getpgrp())
        launch = (
            [sys.executable, str(Path(__file__).resolve()), "phase", *command]
            if owns_group
            else command
        )
        print(f"[{name}] {' '.join(command)}", file=sys.stderr, flush=True)
        try:
            with (
                log_path.open("wb") as log,
                subprocess.Popen(
                    launch,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=os.environ | {RUN_ENV: str(self.path)},
                    start_new_session=owns_group,
                ) as process,
            ):
                exited, wake = os.pipe()

                def wait_for_exit() -> None:
                    process.wait()
                    os.write(wake, b"1")
                    os.close(wake)

                waiter = threading.Thread(target=wait_for_exit)
                waiter.start()
                try:
                    with selectors.DefaultSelector() as streams:
                        streams.register(exited, selectors.EVENT_READ)
                        streams.register(
                            process.stdout, selectors.EVENT_READ, sys.stdout.buffer
                        )
                        streams.register(
                            process.stderr, selectors.EVENT_READ, sys.stderr.buffer
                        )
                        drain_deadline = None
                        while streams.get_map():
                            timeout = (
                                None
                                if drain_deadline is None
                                else max(0, drain_deadline - time.monotonic())
                            )
                            if timeout == 0:
                                break
                            ready = streams.select(timeout)
                            if not ready:
                                break
                            for key, _ in ready:
                                if key.data is None:
                                    streams.unregister(exited)
                                    drain_deadline = time.monotonic() + 1
                                    continue
                                chunk = os.read(key.fd, 65536)
                                if not chunk:
                                    streams.unregister(key.fileobj)
                                    continue
                                log.write(chunk)
                                key.data.write(chunk)
                                key.data.flush()
                        inherited_writers = bool(streams.get_map())
                    if inherited_writers:
                        stop_phase(process, owns_group=owns_group)
                    status = process.wait()
                except BaseException:
                    stop_phase(process, owns_group=owns_group)
                    raise
                finally:
                    waiter.join()
                    os.close(exited)
                    status = process.returncode
        finally:
            failures = []
            with log_path.open(errors="replace") as log:
                for line in log:
                    if match := FAILURE.fullmatch(line.strip()):
                        failures.append(match[1])
            self.record["failing_selectors"] = sorted(
                set(self.record["failing_selectors"] + failures)
            )
            self.record["phases"].append(
                {
                    "name": name,
                    "command": command,
                    "log": str(log_path),
                    "elapsed_seconds": time.monotonic() - started,
                    "exit_status": status,
                }
            )
            self.save()
        return status


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "check-core", "test", "run", "phase"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    options = parser.parse_args()
    if options.mode in {"check", "check-core"} and options.arguments:
        parser.error(f"{options.mode} does not accept arguments")
    if options.mode in {"run", "phase"} and not options.arguments:
        parser.error("run requires a command")
    root = Path(__file__).resolve().parent
    os.chdir(root)
    if options.mode == "phase":
        # The new session has its final group identity here, before exec.
        # Nested workflows keep this group so the outer owner can retire it.
        os.environ[GROUP_ENV] = str(os.getpgrp())
        os.execvp(options.arguments[0], options.arguments)
    core = [
        ("runtime-sources", ["scripts/validate_runtime_sources.py"]),
        ("release-tests", ["tests/release.py"]),
        ("runner-tests", ["tests/transcript_runner.py"]),
        ("workflow-tests", ["python3", "tests/workflow.py"]),
        ("client-tests", ["tests/mcp_client.py"]),
        ("architecture", ["tests/architecture.py"]),
        ("rust-format", ["cargo", "fmt", "--all", "--check"]),
        (
            "clippy",
            [
                "cargo",
                "clippy",
                "--all-targets",
                "--all-features",
                "--",
                "-D",
                "warnings",
            ],
        ),
        ("rust-tests", ["cargo", "test", "--all-targets", "--all-features"]),
    ]
    phases = {
        "check": [
            ("stage", ["scripts/stage-sandbox-runner"]),
            ("core", ["scripts/check-core"]),
            ("transcripts", ["scripts/test"]),
            ("installation", ["python3", "tests/install.py"]),
        ],
        "check-core": core,
        "test": [
            ("build", ["cargo", "build", "--release", "--target-dir", "target"]),
            (
                "transcripts",
                [
                    "uv",
                    "run",
                    "--script",
                    "tests/boundaries/_run.py",
                    *options.arguments,
                ],
            ),
        ],
        "run": [("command", options.arguments)],
    }[options.mode]

    cancelling = False

    def interrupted(number: int, _frame: object) -> None:
        nonlocal cancelling
        if not cancelling:
            cancelling = True
            raise SystemExit(128 + number)

    with ExitStack() as stack:
        stack.enter_context(checkout_owner(root))
        if options.mode in {"check", "test"}:
            stack.enter_context(full_check_slot())
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous = signal.signal(number, interrupted)
            stack.callback(signal.signal, number, previous)
        run = Run(root, sys.argv[1:])
        status = 1
        try:
            for name, command in phases:
                status = 1
                status = run.phase(name, command)
                if status < 0:
                    status = 128 - status
                if status:
                    break
        except SystemExit as error:
            status = error.code
            raise
        except KeyboardInterrupt:
            status = 130
            raise
        finally:
            run.record["exit_status"] = status
            run.save()
            print(f"Validation record: {run.path}", file=sys.stderr, flush=True)
        raise SystemExit(status)


if __name__ == "__main__":
    main()
