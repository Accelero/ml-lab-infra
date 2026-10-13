"""Check subprocess redaction and forced cleanup of commands that overrun."""

import os
import pathlib
import sys
import tempfile
import unittest

import pytest
from support.process import command

from .helpers import expect_equal


class TestProcessTests(unittest.TestCase):
    """Exercise real local subprocesses without external infrastructure."""

    def test_command_error_hides_sensitive_arguments_and_output(self) -> None:
        """A failing command cannot expose its credentials through an exception."""
        with pytest.raises(RuntimeError, match="exit status") as error:
            command(
                [sys.executable, "-c", "import sys; print('private-key'); sys.exit(1)"],
                data="private-input",
            )
        expect_equal("private-key" in str(error.value), expected=False)
        expect_equal("private-input" in str(error.value), expected=False)

    def test_timeout_kills_and_reaps_the_child(self) -> None:
        """A command deadline does not leave a background process behind."""
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "pid"
            with pytest.raises(RuntimeError, match="deadline"):
                command(
                    [
                        sys.executable,
                        "-c",
                        (
                            "import os,pathlib,sys,time; "
                            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
                            "time.sleep(10)"
                        ),
                        str(path),
                    ],
                    timeout=0.5,
                )
            pid = int(path.read_text())
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
