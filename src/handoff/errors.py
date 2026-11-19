"""错误类型。所有对外错误都带一个稳定的 code，CLI 用它决定退出码。"""


class HandoffError(Exception):
    code = "handoff_error"
    exit_code = 1

    def __init__(self, message: str, **context):
        super().__init__(message)
        self.message = message
        self.context = context

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, **self.context}


class ConfigError(HandoffError):
    code = "config_error"
    exit_code = 2


class SchemaError(HandoffError):
    code = "schema_error"
    exit_code = 3

    def __init__(self, message: str, errors: list[str] | None = None, **context):
        super().__init__(message, **context)
        self.errors = errors or []


class TaskNotFound(HandoffError):
    code = "task_not_found"
    exit_code = 4


class TransitionError(HandoffError):
    code = "invalid_transition"
    exit_code = 5


class ConflictError(HandoffError):
    """revision 不匹配或 push 非快进。"""

    code = "revision_conflict"
    exit_code = 6


class GateError(HandoffError):
    code = "gate_failed"
    exit_code = 7

    def __init__(self, message: str, reasons: list[str] | None = None, **context):
        super().__init__(message, **context)
        self.reasons = reasons or []


class GitError(HandoffError):
    code = "git_error"
    exit_code = 8

    def __init__(self, message: str, args=None, stderr="", returncode=None, **context):
        super().__init__(message, **context)
        self.args_ = args
        self.stderr = stderr
        self.returncode = returncode


class LeaseError(HandoffError):
    code = "lease_lost"
    exit_code = 9


class ExecutionError(HandoffError):
    code = "execution_error"
    exit_code = 10
