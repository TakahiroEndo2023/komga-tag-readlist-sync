class KtrsError(Exception):
    """Base class for expected, user-facing KTRS failures."""


class ConfigError(KtrsError):
    pass


class StateError(KtrsError):
    pass


class LockBusyError(KtrsError):
    pass


class ApiError(KtrsError):
    pass


class ApiTransportError(ApiError):
    pass


class ApiResponseError(ApiError):
    pass


class ApiNotFoundError(ApiError):
    pass


class SafetyError(KtrsError):
    pass
