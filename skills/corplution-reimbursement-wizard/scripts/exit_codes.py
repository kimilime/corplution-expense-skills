"""Central command-execution exit codes; workflow state lives in command output."""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    SUCCESS = 0
    OPERATIONAL_ERROR = 1
    COMMAND_ERROR = 2
    REVIEW_REQUIRED = 3
    INTEGRITY_ERROR = 4
    INTERRUPTED = 130
