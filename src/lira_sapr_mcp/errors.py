"""Exceptions shared by all layers. Messages are written for the agent that reads them."""


class LiraError(Exception):
    """Base class for every error the server reports about LIRA."""


class LiraNotInstalledError(LiraError):
    """No supported LIRA installation was found on this computer."""


class LiraTimeoutError(LiraError):
    """LIRA did not answer in time; usually a modal dialog is waiting for the user."""


class LiraComError(LiraError):
    """A COM call into LIRA failed."""


class LiraInputError(LiraError):
    """The agent passed arguments LIRA or the server cannot accept."""
