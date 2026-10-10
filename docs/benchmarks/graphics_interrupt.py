"""Bounded macOS graphics measurements through public MCP send calls."""

import argparse
import ctypes
import json
import os
import platform
import resource
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

if len(sys.argv) > 1 and sys.argv[1] == "--launch":
    resource.setrlimit(resource.RLIMIT_CPU, (30, 35))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024**2, 64 * 1024**2))
    Path(os.environ["LIMIT_REPORT"]).write_text(
        json.dumps(
            {
                "cpu": resource.getrlimit(resource.RLIMIT_CPU),
                "file_size": resource.getrlimit(resource.RLIMIT_FSIZE),
            }
        )
    )
    os.execv(sys.argv[2], sys.argv[2:])

assert sys.platform == "darwin", (
    "this measurement harness uses macOS libproc and sample"
)
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
from support.client import McpClient
from support.processes import (
    capture_process_identity,
    current_process_identity,
    signal_process,
)

OUT: Path
LIB = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
LIB.proc_pidinfo.argtypes = [
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_uint64,
    ctypes.c_void_p,
    ctypes.c_int,
]
LIB.proc_pidinfo.restype = ctypes.c_int
LIB.proc_listchildpids.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
LIB.proc_listchildpids.restype = ctypes.c_int


class TaskInfo(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "virtual",
            "rss",
            "user",
            "system",
            "threads_user",
            "threads_system",
        )
    ] + [("counters", ctypes.c_int32 * 12)]


class Watch:
    def __init__(self, pid: int) -> None:
        self.root = capture_process_identity(pid)
        self.identities = {pid: self.root}
        self.samples = []
        self.error = None
        self.deadline = time.monotonic() + 120
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self) -> None:
        try:
            while not self.stop.is_set():
                tick = time.monotonic()
                pending = [
                    pid
                    for pid, identity in self.identities.copy().items()
                    if current_process_identity(pid) == identity
                ]
                seen = set()
                while pending:
                    pid = pending.pop()
                    if pid in seen:
                        continue
                    seen.add(pid)
                    children = (ctypes.c_int * 256)()
                    ctypes.set_errno(0)
                    count = LIB.proc_listchildpids(
                        pid, children, ctypes.sizeof(children)
                    )
                    assert 0 <= count < 256, (pid, count)
                    assert ctypes.get_errno() in (0, 3), (pid, ctypes.get_errno())
                    for child in children[:count]:
                        if identity := current_process_identity(child):
                            self.identities[child] = identity
                            pending.append(child)
                sizes = {}
                for pid, identity in self.identities.copy().items():
                    if current_process_identity(pid) != identity:
                        continue
                    info = TaskInfo()
                    size = LIB.proc_pidinfo(
                        pid, 4, 0, ctypes.byref(info), ctypes.sizeof(info)
                    )
                    if size == 0:
                        task_error = ctypes.get_errno()
                        if task_error == 3:  # The exiting task is already gone.
                            continue
                        bsd = ctypes.create_string_buffer(136)
                        n = LIB.proc_pidinfo(pid, 3, 1, bsd, len(bsd))
                        if n == 0 or (
                            n == 136
                            and int.from_bytes(bsd.raw[4:8], sys.byteorder) == 5
                        ):
                            continue
                    assert size == ctypes.sizeof(info), (pid, size, ctypes.get_errno())
                    sizes[pid] = info.rss
                self.samples.append([tick, sizes])
                assert max(sizes.values(), default=0) <= 1024**3, sizes
                assert sum(sizes.values()) <= 1536 * 1024**2, sizes
                assert tick < self.deadline, "wall watchdog exceeded"
                self.stop.wait(max(0, 0.020 - (time.monotonic() - tick)))
        except BaseException as error:
            self.error = repr(error)
            for identity in reversed(list(self.identities.values())):
                signal_process(identity, 9)

    def check(self) -> None:
        assert self.error is None, self.error


def text(result: dict) -> str:
    return "".join(part["text"] for part in result["content"] if part["type"] == "text")


def marker(path: Path, watch: Watch) -> list[float]:
    deadline = time.monotonic() + 40
    while not path.exists():
        watch.check()
        assert time.monotonic() < deadline, path
        time.sleep(0.001)
    return list(map(float, path.read_text().split()))


def trial(
    label: str,
    kind: str,
    action: str,
    phase: str = "draw",
    pixels: int = 512,
    vertices: int = 4096,
    backend: str = "quartz",
    sandbox: bool = False,
) -> None:
    assert 0 < pixels <= 2048 and 0 < vertices <= 65536
    assert phase != "close" or kind in {"raster", "raster-explicit", "lines"}
    report = dict(
        label=label,
        kind=kind,
        action=action,
        phase=phase,
        pixels=pixels,
        vertices=vertices,
        backend=backend,
        sandbox=sandbox,
    )
    with tempfile.TemporaryDirectory(prefix="graphics-audit-") as tmp:
        directory = Path(tmp)
        environment = os.environ | {
            "TMPDIR": tmp,
            "LIMIT_REPORT": str(directory / "limits.json"),
        }
        args = ["serve"] + (["--writable-root", tmp] if sandbox else ["--no-sandbox"])
        client = McpClient(
            Path(sys.executable),
            (
                str(Path(__file__).resolve()),
                "--launch",
                str(ROOT / "target/release/mcp-console"),
                *args,
            ),
            environment,
            current_directory=directory,
            response_timeout=50,
        )
        watch = None
        sampler = None
        try:
            client.initialize_and_list_tools()
            setup = f'''
options(bitmapType = "{backend}", console.plot.width_in = {pixels}/96,
        console.plot.height_in = {pixels}/96, console.plot.dpi = 96)
survivor <- 42L
mark <- function(name) {{
  path <- paste0(name, ".tmp")
  cat(format(c(as.numeric(Sys.time()), proc.time()[[1]], proc.time()[[2]]),
             digits = 17), file = path, sep = " ")
  stopifnot(file.rename(path, name))
  invisible(NULL)
}}
x <- ((seq_len({vertices}) * 8191) %% 65521) / 65521
y <- ((seq_len({vertices}) * 32749) %% 65521) / 65521
stopifnot(length(x) == {vertices}, length(y) == {vertices})
if (grepl("^raster", "{kind}")) {{
  values <- ((seq_len(1024L * 1024L) * 8191) %% 16777213)
  picture <- as.raster(matrix(sprintf("#%06x", values), 1024L, 1024L))
  stopifnot(identical(dim(picture), c(1024L, 1024L)))
}}
invisible(trace("dev.off", where = asNamespace("grDevices"),
  tracer = quote(.GlobalEnv$mark("close_enter")),
  exit = quote(.GlobalEnv$mark("close_exit")), print = FALSE))
cat(Sys.getpid(), tempdir(), R.version.string, R.version$platform,
    getOption("bitmapType"), sep = "\\n")
'''
            meta = text(
                client.send(
                    r='cat(Sys.getpid(), tempdir(), R.version.string, R.version$platform, getOption("bitmapType"), sep = "\\n")'
                )
            )
            report["metadata"] = meta
            lines = meta.splitlines()
            pid = int(next(line for line in lines if line.isdigit()))
            worker = capture_process_identity(pid)
            report["worker"] = worker
            r_tmp = Path(next(line for line in lines if "/Rtmp" in line))
            report["r_tmp"] = str(r_tmp)
            caps = text(
                client.send(
                    python="import resource, os; print(os.getpid(), resource.getrlimit(resource.RLIMIT_CPU), resource.getrlimit(resource.RLIMIT_FSIZE))"
                )
            )
            report["worker_limits"] = caps
            assert (
                str(pid) in caps
                and "(30, 35)" in caps
                and "(67108864, 67108864)" in caps
            ), caps
            assert json.loads((directory / "limits.json").read_text()) == {
                "cpu": [30, 35],
                "file_size": [67108864, 67108864],
            }
            watch = Watch(client.process.pid)
            setup_result = client.send(r=setup)
            assert setup_result.get("isError") is not True, text(setup_result)
            assert "[running" not in text(setup_result), text(setup_result)
            report["configured_metadata"] = text(setup_result)
            sample_path = OUT / f"{label}.sample.txt"
            sampler = subprocess.Popen(
                ["/usr/bin/sample", str(pid), "1", "1", "-file", str(sample_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            draw = {
                "lines": "lines(x, y, lwd = 1)",
                "raster": "rasterImage(picture, 0, 0, 1, 1, interpolate = FALSE)",
                "sleep": "Sys.sleep(60)",
                "plot-sleep": "Sys.sleep(60)",
                "raster-explicit": "rasterImage(picture, 0, 0, 1, 1, interpolate = FALSE)",
            }[kind]
            program = (
                ""
                if kind == "sleep"
                else "plot.new(); plot.window(c(0, 1), c(0, 1));\n"
            ) + f'mark("draw_enter"); {draw}; mark("draw_exit")'
            if kind == "raster-explicit":
                program += "; invisible(grDevices::dev.off())"
            watch.deadline = time.monotonic() + 45
            admitted_at = time.monotonic()
            result = client.send(r=program, timeout_ms=0)
            report["admission_ms"] = 1000 * (time.monotonic() - admitted_at)
            if action != "complete":
                try:
                    report["phase_enter"] = marker(directory / f"{phase}_enter", watch)
                except AssertionError:
                    report["marker_failure_output"] = text(client.send(timeout_ms=0))
                    raise
                control_wall = time.time()
                control_at = time.monotonic()
                result = client.send(control=action, timeout_ms=0)
                report["control_wall"] = control_wall
                report["control_ms"] = 1000 * (time.monotonic() - control_at)
                report["control_text"] = text(result)
            outputs = [result]
            while "[running" in text(result) or "[worker starting]" in text(result):
                watch.check()
                result = client.send(timeout_ms=30000)
                outputs.append(result)
            settled_at = time.monotonic()
            report["evaluation_ms"] = 1000 * (settled_at - admitted_at)
            if action != "complete":
                report["settlement_after_control_ms"] = 1000 * (settled_at - control_at)
            report["settled_text"] = "".join(map(text, outputs))
            report["images"] = sum(
                part["type"] == "image"
                for result in outputs
                for part in result["content"]
            )
            report["markers"] = {
                path.name: list(map(float, path.read_text().split()))
                for path in directory.iterdir()
                if path.name in {"draw_enter", "draw_exit", "close_enter", "close_exit"}
            }
            report["old_worker_live"] = current_process_identity(pid) == worker
            report["old_r_tmp_exists"] = r_tmp.exists()
            report["managed_pngs"] = [
                str(path) for path in (r_tmp / "mcp-console-plots").glob("*.png")
            ]
            report["remaining_pngs"] = [
                str(path.relative_to(directory)) for path in directory.rglob("*.png")
            ]
            recovery = client.send(
                r='cat(Sys.getpid(), exists("survivor", inherits = FALSE), is.null(dev.list()), sep = " "); plot(1:3)'
            )
            report["recovery_text"] = text(recovery)
            report["recovery_images"] = sum(
                part["type"] == "image" for part in recovery["content"]
            )
            assert report["recovery_images"] == 1, report
            client.finish()
        finally:
            client.close()
            if sampler:
                sampler.wait(timeout=5)
            assert watch is not None
            watch.stop.set()
            watch.thread.join(timeout=3)
            report["watch_error"] = watch.error
            report["max_process_rss_mib"] = (
                max(
                    (max(sizes.values(), default=0) for _, sizes in watch.samples),
                    default=0,
                )
                / 1024**2
            )
            report["max_tree_rss_mib"] = (
                max((sum(sizes.values()) for _, sizes in watch.samples), default=0)
                / 1024**2
            )
            intervals = [b[0] - a[0] for a, b in zip(watch.samples, watch.samples[1:])]
            report["sample_interval_ms"] = (
                [1000 * sum(intervals) / len(intervals), 1000 * max(intervals)]
                if intervals
                else []
            )
            exit_deadline = time.monotonic() + 3
            while (
                any(
                    current_process_identity(identity[0]) == identity
                    for identity in watch.identities.values()
                )
                and time.monotonic() < exit_deadline
            ):
                time.sleep(0.02)
            report["live_after_close"] = [
                identity
                for identity in watch.identities.values()
                if current_process_identity(identity[0]) == identity
            ]
            (OUT / f"{label}.json").write_text(json.dumps(report, indent=2))
            (OUT / f"{label}.calls.json").write_text(
                json.dumps(client.transcript, indent=2)
            )
            assert not report["live_after_close"], report["live_after_close"]
            watch.check()
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("label")
    parser.add_argument(
        "kind", choices=("lines", "raster", "raster-explicit", "sleep", "plot-sleep")
    )
    parser.add_argument("action", choices=("complete", "interrupt", "restart"))
    parser.add_argument("--phase", choices=("draw", "close"), default="draw")
    parser.add_argument("--pixels", type=int, default=512)
    parser.add_argument("--vertices", type=int, default=4096)
    parser.add_argument("--backend", choices=("quartz", "cairo"), default="quartz")
    parser.add_argument("--sandbox", action="store_true")
    options = parser.parse_args()
    OUT = options.output.resolve()
    OUT.mkdir(parents=True, exist_ok=True)
    assert not (OUT / f"{options.label}.json").exists(), "use a new trial label"
    print(
        json.dumps(
            {
                "host": platform.platform(),
                "machine": platform.machine(),
                "revision": subprocess.check_output(
                    ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True
                ).strip(),
            }
        ),
        flush=True,
    )
    trial(
        options.label,
        options.kind,
        options.action,
        options.phase,
        options.pixels,
        options.vertices,
        options.backend,
        options.sandbox,
    )
