"""The argument parser itself: help must format, and `stop` must exist.

There was no test file for the parser until `muninn serve --help` was found
crashing — in a released build, on the daemon's own command:

    TypeError: %o format: an integer is required, not dict

argparse formats every help string through ``help % params`` to expand
``%(default)s``, and one of `serve`'s strings said "100% of eligible". ``"% o"``
is a valid printf conversion (space flag, octal), so the expansion tried to
render a dict as octal and took the whole command down. Nothing caught it
because no test had ever asked argparse to *format* the help — building the
parser is not enough, and `--help` is the one command nobody writes a test for.
"""
from __future__ import annotations

import unittest

from muninn import cli


def _subparsers(parser):
    """Every registered subcommand, as (name, parser) pairs."""
    import argparse

    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            yield from action.choices.items()


class HelpFormattingTest(unittest.TestCase):
    """Every command's help must render, not just parse.

    A contract test in the sense that matters here: argparse is the other half
    and it is the half that does the `%` expansion, so the only way to assert
    this is to make argparse do the work. Asserting on the strings ourselves
    would re-implement the bug's hiding place.
    """

    def test_the_top_level_help_formats(self) -> None:
        self.assertIn("usage:", cli.build_parser().format_help())

    def test_every_subcommand_help_formats(self) -> None:
        """The one that would have caught the released crash.

        Subtested per command so a failure names the command rather than
        making someone bisect the parser.
        """
        names = []
        for name, parser in _subparsers(cli.build_parser()):
            names.append(name)
            with self.subTest(command=name):
                # format_help(), not print_help(): same expansion, no output.
                self.assertIn("usage:", parser.format_help())
        # A guard against this test passing because it iterated nothing — the
        # private _actions walk above is exactly the kind of introspection that
        # silently returns empty if argparse rearranges its internals.
        self.assertGreater(len(names), 10,
                           f"only found {names}; the subparser walk broke")

    def test_a_literal_percent_survives_formatting(self) -> None:
        """Pinned on the specific string that broke, so a revert is caught.

        `serve`'s --no-recalibrate help is the one that said "100% of
        eligible". The assertion is on the *rendered* text containing a single
        percent, which is true only if the source doubled it.
        """
        commands = dict(_subparsers(cli.build_parser()))
        rendered = commands["serve"].format_help()
        self.assertIn("100% of eligible", rendered)


class StopCommandTest(unittest.TestCase):
    """`muninn stop` exists and defaults the way a script would expect."""

    def test_stop_is_registered(self) -> None:
        self.assertIn("stop", dict(_subparsers(cli.build_parser())))

    def test_it_stops_rather_than_restarts_by_default(self) -> None:
        args = cli.build_parser().parse_args(["stop"])
        self.assertFalse(args.restart)
        self.assertIs(args.func, cli.cmd_stop)

    def test_restart_is_the_same_command_with_a_flag(self) -> None:
        """Not a separate verb: both are action ids the daemon already
        publishes, and keeping them one command keeps the teardown one path."""
        args = cli.build_parser().parse_args(["stop", "--restart"])
        self.assertTrue(args.restart)


if __name__ == "__main__":
    unittest.main()
