"""Command-line entry point: ``lira-sapr-mcp`` runs the MCP server over stdio."""

import argparse
import logging
import sys
from pathlib import Path

from lira_sapr_mcp import __version__


def setup_logging(log_file) -> None:
    # stdout carries the MCP protocol in stdio mode, so the log goes to a file.
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=log_file,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        encoding="utf-8",
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    # An unknown flag stops the program: a mistyped --no-scripts must not leave the
    # script tool on unnoticed.
    parser = argparse.ArgumentParser(
        prog="lira-sapr-mcp",
        description="MCP server for LIRA-SAPR over stdio; an MCP client starts it.",
        allow_abbrev=False,
    )
    parser.add_argument("--version", action="version", version=f"lira-sapr-mcp {__version__}")
    parser.add_argument(
        "--no-scripts", action="store_true", help="leave out lira_run_script (the agent's Python)"
    )
    parser.add_argument(
        "--diagnose",
        nargs="?",
        const="",
        metavar="PATH",
        help="write a report on the LIRA releases of this computer (file or folder; "
        "default: the current folder) and exit; LIRA is not started",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args(sys.argv[1:])
    if args.diagnose is not None:
        from lira_sapr_mcp.diagnose import write_report

        try:
            target = write_report(Path(args.diagnose) if args.diagnose else None)
        except OSError as exc:
            print(f"lira-sapr-mcp: Cannot write the report: {exc}", file=sys.stderr)
            sys.exit(2)
        print(f"Report written to {target}")
        return
    from lira_sapr_mcp.config import Settings, load_settings
    from lira_sapr_mcp.errors import LiraInputError
    from lira_sapr_mcp.server import build_server
    from lira_sapr_mcp.services.session import LiraSession

    try:
        settings = load_settings()
    except LiraInputError as exc:
        # Before the server runs nobody else sees it: the default log and stderr, which
        # MCP clients show in their server logs.
        setup_logging(Settings().log_file)
        logging.getLogger(__name__).error("%s", exc)
        print(f"lira-sapr-mcp: {exc}", file=sys.stderr)
        sys.exit(2)
    setup_logging(settings.log_file)
    logging.getLogger(__name__).info("lira-sapr-mcp %s starting", __version__)
    scripts = not args.no_scripts
    logging.getLogger(__name__).info("scripts %s", "on" if scripts else "off (--no-scripts)")
    build_server(LiraSession(settings), scripts=scripts).run("stdio")


if __name__ == "__main__":
    main()
