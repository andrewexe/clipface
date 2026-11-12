"""Errors every platform publisher uses, so the scheduler can treat platforms alike."""


class PublishError(Exception):
    """A temporary problem: the job is retried later."""


class PublishAuthError(PublishError):
    """The account's sign-in no longer works; the user has to reconnect it."""


class PublishRejected(PublishError):
    """The platform refused this post for good. Retrying will not help."""

    def __init__(self, message: str, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


class PublishPending(PublishError):
    """Nothing is wrong: the platform is still processing. Look again shortly."""
