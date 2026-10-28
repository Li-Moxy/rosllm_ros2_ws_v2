"""Information codes returned by ROSLLM behaviour executors.

Values 0, 1 and -1..-6 are taken from the upstream
``behavior_executor/src/behavior_executor/info.py``.  Positive values are
warnings, negative values are failures.  Values -7..-13 are added by this port
so that callers can distinguish "failed" from "timed out" and "state unknown".
"""

from enum import IntEnum


class Info(IntEnum):
    OK = 0
    NULL_BEHAVIOR = 1

    FAILED_TO_COMPILE = -1
    ATOMIC_ACTION_UNAVAILABLE = -2
    CODE_IMPORT_ERROR = -3
    CODE_EXECUTION_ERROR = -4
    SAVE_CODE_FAILED = -5
    ERROR_ATOMIC_ACTION = -6

    # Added by the ROS 2 port.
    INVALID_BEHAVIOR = -7
    BUSY = -8
    TIMEOUT = -9
    UNKNOWN_STATE = -10
    NOT_VERIFIED = -11
    LIMIT_EXCEEDED = -12
    STOPPED = -13
