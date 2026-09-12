"""Real TCP stalls controlled by a acknowledged Unix socket, never timing sleeps."""

import os
from pathlib import Path
import select
import socket
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from support.events import Events

parent_exit = Events()
parent_exit.watch_process(os.getppid())
parent_gone, notify_parent = os.pipe()


def watch_parent():
    parent_exit.wait(600)
    os.close(notify_parent)


threading.Thread(target=watch_parent, daemon=True).start()

root, role, host, port = sys.argv[1:]
endpoint = Path(root) / (role + ".sock")
endpoint.unlink(missing_ok=True)
control = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
control.bind(str(endpoint))
control.listen()
network = socket.create_connection((host, int(port)))
network.setblocking(False)
os.set_blocking(0, False)
os.set_blocking(1, False)
up, down = bytearray(), bytearray()
paused = set()
while True:
    readers = [control, parent_gone]
    if "up" not in paused and len(up) < 65536:
        readers.append(0)
    if "down" not in paused and len(down) < 65536:
        readers.append(network)
    writers = []
    if up and "up" not in paused:
        writers.append(network)
    if down and "down" not in paused:
        writers.append(1)
    poller = select.poll()
    masks = {0: 0, 1: 0}
    descriptor = lambda value: value if isinstance(value, int) else value.fileno()
    for value in readers:
        fd = descriptor(value)
        masks[fd] = masks.get(fd, 0) | select.POLLIN
    for value in writers:
        fd = descriptor(value)
        masks[fd] = masks.get(fd, 0) | select.POLLOUT
    for fd, mask in masks.items():
        poller.register(fd, mask)
    ready = dict(poller.poll())
    if ready.get(parent_gone, 0):
        break
    if any(ready.get(fd, 0) & (select.POLLHUP | select.POLLERR) for fd in (0, 1)):
        break
    readable = [
        value for value in readers if ready.get(descriptor(value), 0) & select.POLLIN
    ]
    writable = [
        value for value in writers if ready.get(descriptor(value), 0) & select.POLLOUT
    ]
    if control in readable:
        with control.accept()[0] as request:
            action = request.recv(100).decode()
            if action == "close":
                request.sendall(b"ok")
                break
            paused = set() if action == "resume" else set(action.split())
            assert paused <= {"up", "down"}, paused
            request.sendall(b"ok")
    if 0 in readable:
        data = os.read(0, 65536 - len(up))
        if not data:
            break
        up.extend(data)
    if network in readable:
        data = network.recv(65536 - len(down))
        if not data:
            break
        down.extend(data)
    if network in writable:
        del up[: network.send(up)]
    if 1 in writable:
        del down[: os.write(1, down)]
