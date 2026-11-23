"""Pytest config: the CLI tests need r3 + click + yaml (the toolchain venv); without them
they are not collected. Shared fixtures/helpers live in cli_support.py."""
try:
    import r3  # noqa: F401
    import click  # noqa: F401
    import yaml  # noqa: F401
except ImportError:
    collect_ignore_glob = ["test_cli_*.py"]
else:
    from cli_support import env  # noqa: F401  (fixture)
