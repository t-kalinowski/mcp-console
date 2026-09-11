"""Explicit proxy inputs required by the pinned native runner."""

NATIVE_PROXY = {
    "enabled": True,
    "enableSocks5": True,
    "enableSocks5Udp": False,
    "allowUpstreamProxy": False,
    "dangerouslyAllowAllUnixSockets": False,
    "mode": "full",
    "allowLocalBinding": False,
}
