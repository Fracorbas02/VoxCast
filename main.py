#!/usr/bin/env python3
"""
VoxCast - Point d'entrée principal
GTK4 + Libadwaita pour une interface moderne et native sous Linux.
"""

import sys

from window import VoxCastWindow


def main():
    app = VoxCastWindow()
    sys.exit(app.run())


if __name__ == "__main__":
    main()
