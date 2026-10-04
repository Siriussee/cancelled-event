"""Discover cancellations, match snapshots and verify listing prices."""

import argparse
import importlib


def main(argv=None):
    parser = argparse.ArgumentParser(prog="cancelled-event", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect", help="Collect a platform event snapshot.")
    sources = collect.add_subparsers(dest="platform", required=True)
    for name, module, help_text in (
        ("stubhub", "sources.stubhub_events", "Collect location-based StubHub events."),
        (
            "ticketmaster",
            "sources.ticketmaster_cancellations",
            "Collect Ticketmaster cancelled events.",
        ),
    ):
        command = sources.add_parser(name, add_help=False, help=help_text)
        command.set_defaults(module=module)
    for name, module, help_text in (
        ("match", "matching", "Match existing StubHub and Ticketmaster snapshots."),
        ("prices", "pricing.refresh", "Verify matched events' listing prices in USD."),
        ("run", "workflow", "Match existing snapshots and verify prices in one run."),
        ("venues", "sources.venue_maps", "Enrich StubHub events with venue maps."),
        ("filter-cities", "sources.city_seeds", "Filter city seeds using GeoNames features."),
        (
            "verify-ticketmaster",
            "diagnostics.ticketmaster_api",
            "Check official Ticketmaster API access.",
        ),
    ):
        command = commands.add_parser(name, add_help=False, help=help_text)
        command.set_defaults(module=module)
    args, remaining = parser.parse_known_args(argv)
    handler = importlib.import_module(f"cancelled_event.{args.module}").main
    try:
        return handler(remaining) or 0
    except KeyboardInterrupt:
        return 130
