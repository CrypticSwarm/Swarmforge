"""Shared Python for Swarmforge's host launcher and its container-side tooling.

Modules here are importable from both sides of the container boundary: the
launcher runs them from the checkout, and the image copies the package in. The
package carries no third-party dependencies, because the harness image installs
none and the launcher runs on whatever python3 the host provides.
"""
