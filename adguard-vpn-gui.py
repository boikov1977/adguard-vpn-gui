#!/usr/bin/python3
"""AdGuard VPN GUI launcher."""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from agvpn.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv))
