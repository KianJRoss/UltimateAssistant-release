"""Stable exception hierarchy for Herald package consumers."""


class HeraldError(RuntimeError):
    """Base class for package-level Herald failures."""


class RouterUnavailableError(HeraldError):
    """The configured router could not be reached or started."""


class CLIAccountInvocationError(HeraldError):
    """A named CLI account could not complete an inference request."""


class InvalidResponseError(HeraldError):
    """A model or router response did not match the requested structure."""


class DecisionValidationError(InvalidResponseError):
    """A decision was not one of the permitted choices or schema."""


class SessionBusyError(HeraldError):
    """A stateful sequential memory already has a call in progress."""
