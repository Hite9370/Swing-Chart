class FeedError(Exception):
    """A price feed problem with a message that is safe to show to the user."""


class RateLimited(FeedError):
    """The broker is limiting how fast we ask for data. Waiting helps, asking for less does not."""
