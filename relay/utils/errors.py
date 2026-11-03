class RelayError(Exception):
    code = "error"

    def __init__(self, message: str, *, code: str | None = None, user_message: str | None = None):
        super().__init__(message)
        if code:
            self.code = code
        self.user_message = user_message or message


class ConfigError(RelayError):
    code = "config_error"


class StorageError(RelayError):
    code = "storage_error"


class StorageCorruption(StorageError):
    code = "storage_corrupt"


class AuditFailure(StorageError):
    code = "audit_failure"


class Denied(RelayError):
    code = "denied"


class PlanError(RelayError):
    code = "bad_plan"


class Unresolved(RelayError):
    code = "unresolved"


class Ambiguous(RelayError):
    code = "ambiguous"

    def __init__(self, kind: str, ref: str, candidates: list[tuple[int, str]], suggestion: bool = False):
        super().__init__(f"'{ref}' is ambiguous")
        self.kind = kind
        self.ref = ref
        self.candidates = candidates
        self.suggestion = suggestion


class RateLimited(RelayError):
    code = "rate_limited"

    def __init__(self, message: str, retry_after: float = 0):
        super().__init__(message)
        self.retry_after = retry_after


class AIError(RelayError):
    code = "ai_error"


class ExecError(RelayError):
    code = "exec_failed"


class Retryable(ExecError):
    code = "retryable"


class Uncertain(ExecError):
    code = "uncertain"
