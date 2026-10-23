"""Compatibility entry point; use pokerDemo server."""

import sys

from pokerdemo.cli import app

if __name__ == "__main__":
    app(["server", *sys.argv[1:]])
