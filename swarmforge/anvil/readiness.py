"""Waiting for a started tong to report ready.

The anvil must not run against a half-up environment, so each tong is probed in
the mode its definition resolves to before the launch continues: dialing the
alias the anvil will reach it at, running its healthcheck, or -- for a tong that
declares neither -- not at all.
"""

import time

from swarmforge import tongs


def wait_ready(docker, container, defn, alias, network, *, anvil_image,
               sleep=time.sleep, monotonic=time.monotonic, interval=0.5):
    """Block until a tong reports ready, returning True/False on timeout.

    Dispatches on the tong's resolved readiness mode (see
    `tongs.readiness_settings`): `tcp` dials the canonical alias on the network;
    `healthcheck` runs the declared exec command or polls the image HEALTHCHECK;
    `none` is treated as ready immediately. A `tcp` probe dials the tong's
    network-internal port from a throwaway container, which needs both a network
    to dial on and the anvil image to run from; without either it degrades to "is
    the container running" -- decided and warned once, not on every poll.
    """
    mode, command, timeout_s = tongs.readiness_settings(defn)
    if mode == "none":
        return True

    interface = defn.get("interface") or {}
    port = interface.get("port")

    tcp_degraded = mode == "tcp" and (not anvil_image or not network)
    if tcp_degraded:
        tongs.warn(
            "cannot run a TCP readiness probe of '%s' (no anvil image or "
            "network); falling back to a container-running check" % container
        )

    def probe():
        if mode == "tcp":
            if tcp_degraded:
                state = docker.inspect_state(container)
                return bool(state and state["running"])
            return docker.tcp_probe(network, alias, port, anvil_image)
        # healthcheck
        if command:
            return docker.exec_ok(container, command)
        return docker.health_status(container) == "healthy"

    start = monotonic()
    while True:
        if probe():
            return True
        if monotonic() - start >= timeout_s:
            return False
        sleep(interval)
