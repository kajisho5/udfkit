"""The one exception udfkit raises."""


class UdfError(Exception):
    """An image cannot be written (or read) as asked. `hint` says what to do
    about it, when there is something to say."""

    def __init__(self, message, hint=None):
        super().__init__(message)
        self.message = message
        self.hint = hint
