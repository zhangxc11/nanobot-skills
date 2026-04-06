#!/usr/bin/env python3
"""brain_manager.py — Compatibility shim. Use task_store.py instead."""
import warnings as _w
_w.warn("brain_manager.py is deprecated, use task_store.py", DeprecationWarning, stacklevel=2)
from task_store import *  # noqa: F401,F403
from task_store import main  # noqa: F401

if __name__ == "__main__":
    main()
