"""PyInstaller entry point for the desktop app (frozen apps need an absolute import)."""
from locklyctl.gui.app import main

raise SystemExit(main())
