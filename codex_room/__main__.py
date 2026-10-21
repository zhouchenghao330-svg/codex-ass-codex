"""Support both `python -m codex_room` and running the package directory."""
if not __package__:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from codex_room.app import main

if __name__ == "__main__":
    main()
