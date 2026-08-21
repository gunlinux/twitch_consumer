"""Domain errors."""


class OAuthFlowError(RuntimeError):
    """The OAuth callback cannot be completed (bad state or Twitch error)."""
