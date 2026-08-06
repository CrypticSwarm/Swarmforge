"""Resolving a tong's secret references on the host, and delivering them to it.

Resolution shells out to the provider CLIs declared in the user-layer table, so
an interactive unlock happens in the user's terminal before the anvil starts.
Delivery never lets the resolved value become a docker `-e` env var (anything
holding the docker socket could read it back), a command-line argument, or a
file on disk: it reaches the tong through a host FIFO, so the bytes only ever
live in the kernel pipe buffer.

Both halves are the impure counterpart to the pure planning in
`swarmforge.tongs`, which decides what to resolve and what shell the tong runs.
"""

import errno
import os
import shutil
import subprocess
import tempfile
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
# Resolved secrets reach a tong over a host FIFO bind-mounted into the container,
# not as `-e`/argv/disk. The bytes only ever live in the kernel pipe buffer: the
# tong's `/bin/sh` wrapper blocks reading the FIFO, and the launcher writes the
# `export NAME=value` script once the wrapper has opened the read end, so the
# values are in the process environment before the real entrypoint starts. The
# channel is created behind a factory so `run_with_tongs` can be tested with a
# fake that records the payload instead of touching the filesystem.


class SecretChannel:
    """A host FIFO handing a tong its secret env through the kernel pipe buffer."""

    def __init__(self, directory, host_path):
        self._dir = directory
        self.host_path = host_path

    def deliver(self, payload, *, timeout=30.0, poll=0.05,
                sleep=time.sleep, monotonic=time.monotonic):
        """Write `payload` once the tong has opened the FIFO's read end.

        Opens the write end non-blocking and retries while no reader is attached
        (`ENXIO`), so a tong that never starts times out rather than hanging the
        launcher forever. Once the tong's wrapper has opened the read end the open
        succeeds; the whole payload is written -- looping over partial writes and
        a full pipe buffer, since the channel is non-blocking and a payload can
        exceed the pipe capacity -- and the end closed, signalling EOF so the
        wrapper's `cat` returns and it execs the real process. Raises
        `OrchestrationError` if no reader appears, the buffer never drains within
        `timeout`, or the tong closes the read end before delivery completes (so a
        truncated secret never reaches the tong silently).
        """
        deadline = monotonic() + timeout
        while True:
            try:
                fd = os.open(self.host_path, os.O_WRONLY | os.O_NONBLOCK)
                break
            except OSError as exc:
                if exc.errno == errno.ENXIO and monotonic() < deadline:
                    sleep(poll)
                    continue
                if exc.errno == errno.ENXIO:
                    raise OrchestrationError(
                        "tong did not open its secret channel within %gs" % timeout
                    )
                raise OrchestrationError("secret channel error: %s" % exc)
        try:
            data = memoryview(payload.encode("utf-8"))
            while data:
                try:
                    data = data[os.write(fd, data):]
                except BlockingIOError:
                    # Pipe buffer full; the tong's `cat` is still draining. Wait,
                    # bounded by the same deadline, rather than dropping bytes.
                    if monotonic() >= deadline:
                        raise OrchestrationError(
                            "tong did not drain its secret channel within %gs" % timeout
                        )
                    sleep(poll)
                except BrokenPipeError:
                    raise OrchestrationError(
                        "tong closed its secret channel before delivery completed"
                    )
        finally:
            os.close(fd)

    def cleanup(self):
        """Remove the FIFO and its directory (best-effort)."""
        shutil.rmtree(self._dir, ignore_errors=True)


def open_secret_channel(uid=None):
    """Create a host FIFO in a private temp dir, returning a `SecretChannel`.

    The directory is mode 0700 and the FIFO 0600, so only the launcher's user can
    open the read end and intercept the secret. When the tong runs as a different
    non-root uid (from the image config) the FIFO is `chown`ed to it best-effort so
    that user can read it; a container running as root reads it regardless.
    """
    directory = tempfile.mkdtemp(prefix="swarmforge-secret-")
    os.chmod(directory, 0o700)
    host_path = os.path.join(directory, "secret-env")
    os.mkfifo(host_path, 0o600)
    if uid is not None:
        try:
            os.chown(host_path, uid, -1)
        except OSError:
            pass  # not permitted (uid differs and launcher is not root); 0600 stands
    return SecretChannel(directory, host_path)


def _uid_of(image_user):
    """Numeric uid from an image's configured user, or None if not a bare uid.

    `docker inspect`'s `.Config.User` may be empty, a numeric `uid[:gid]`, or a
    name. Only a bare numeric uid can be `chown`ed to without a passwd lookup; a
    name (or empty/root) leaves the FIFO at its default 0600 launcher ownership.
    """
    if not image_user:
        return None
    token = image_user.split(":", 1)[0]
    try:
        return int(token)
    except ValueError:
        return None
