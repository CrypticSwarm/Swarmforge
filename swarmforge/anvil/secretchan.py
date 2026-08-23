"""Resolving a tong's secret references on the host, and delivering them to it.

Resolution shells out to the provider CLIs declared in the user-layer table, so
an interactive unlock happens in the user's terminal before the anvil starts.
Delivery never lets the resolved value become a docker `-e` env var (anything
holding the docker socket could read it back), a command-line argument, or a
file on disk: it is streamed over `docker exec -i` stdin into a FIFO the tong's
own wrapper creates on an in-container tmpfs, so the bytes only ever live in
docker's API stream and the container kernel's pipe buffer.

Both halves are the impure counterpart to the pure planning in
`swarmforge.tongs`, which decides what to resolve and what shell the tong runs.
"""

import subprocess
import time

from swarmforge import tongs

from .errors import OrchestrationError


# --- Secret resolution --------------------------------------------------------


class SecretResolutionError(Exception):
    """A secret reference could not be resolved; the launch must not proceed."""


def make_secret_resolver(providers):
    """Build the impure resolver closure over a configured provider table.

    Returns `resolve(provider, ref) -> str`, the side-effectful counterpart to
    the pure `tongs.substitute_secrets`/`tongs.plan_tong_secrets`: it shells out
    to the provider CLI built by `tongs.secret_provider_command` and returns the
    secret printed on stdout. Interactive unlocks (`op signin`, biometrics) work
    because the launcher runs in the user's terminal before the anvil starts. A
    single trailing newline -- which provider CLIs conventionally append -- is
    stripped; any other whitespace is preserved verbatim.

    Raises `SecretResolutionError` (naming the provider and reference, never the
    secret) for an unknown provider, a CLI that cannot be run, or a non-zero
    exit, so a misconfigured secret stops the launch rather than handing the tong
    an empty or partial value.
    """

    def resolve(provider, ref):
        try:
            command = tongs.secret_provider_command(providers, provider, ref)
        except KeyError:
            raise SecretResolutionError(
                "no secret provider %r is configured; declare it in "
                "secret-providers.yaml" % provider
            )
        except tongs.UnmappedSecretError:
            raise SecretResolutionError(
                "secret provider %r maps no command for %r; add it (or a "
                "'default') under that provider in secret-providers.yaml"
                % (provider, ref)
            )
        try:
            completed = subprocess.run(command, stdout=subprocess.PIPE, check=False)
        except OSError as exc:
            raise SecretResolutionError(
                "secret provider %r could not run: %s" % (provider, exc)
            )
        if completed.returncode != 0:
            raise SecretResolutionError(
                "secret provider %r failed for %r (exit %d)"
                % (provider, ref, completed.returncode)
            )
        value = completed.stdout.decode("utf-8")
        return value[:-1] if value.endswith("\n") else value

    return resolve


# --- Secret delivery channel --------------------------------------------------
# Resolved secrets reach a tong over a FIFO its own wrapper creates on an
# in-container tmpfs, not as `-e`/argv/disk (a host FIFO would not survive
# Docker Desktop's VM boundary; see "Secret delivery" in swarmforge.tongs.secrets).
# The launcher streams the `export NAME=value` script through `docker exec -i`,
# and the tong's `/bin/sh` wrapper blocks reading the FIFO, so the values are in
# the process environment before the real entrypoint starts. The channel is
# created behind a factory so `run_with_tongs` can be tested with a fake that
# records the payload instead of invoking docker.


class SecretChannel:
    """Streams a tong's secret env into its in-container FIFO via docker exec."""

    def __init__(self, docker, container):
        self._docker = docker
        self._container = container

    def deliver(self, payload, *, timeout=30.0, poll=0.05,
                sleep=time.sleep, monotonic=time.monotonic):
        """Stream `payload` into the tong's FIFO once its wrapper has created it.

        Runs `tongs.secret_deliver_command` under `docker exec -i` with `payload`
        on stdin, retrying while the wrapper has not run `mkfifo` yet
        (`SECRET_FIFO_ABSENT_EXIT`), so a tong that never reaches its wrapper
        times out rather than hanging the launcher forever. Once the FIFO exists
        the payload is copied through (`secret_deliver_command` describes the
        blocking/EOF handshake). Raises
        `OrchestrationError` if the FIFO never appears or the payload is never
        accepted within `timeout`, or if the exec fails outright (a tong that
        exited before delivery), so a truncated secret never reaches the tong
        silently.
        """
        deadline = monotonic() + timeout
        data = payload.encode("utf-8")
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise OrchestrationError(
                    "tong did not open its secret channel within %gs" % timeout
                )
            code, stderr = self._docker.exec_stdin(
                self._container, tongs.secret_deliver_command(), data,
                timeout=remaining,
            )
            if code == 0:
                return
            if code is None:
                raise OrchestrationError(
                    "tong did not accept its secret delivery within %gs" % timeout
                )
            if code == tongs.SECRET_FIFO_ABSENT_EXIT:
                sleep(poll)
                continue
            raise OrchestrationError(
                "secret delivery failed (docker exec exit %d): %s" % (
                    code,
                    stderr or "the tong may have exited before delivery completed",
                )
            )
