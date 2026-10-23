"""Compatibility entry point; use pokerDemo client."""

import sys

from pokerdemo.cli import app

if __name__ == "__main__":
    app(["client", *sys.argv[1:]])
