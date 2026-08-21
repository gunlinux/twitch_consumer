"""Twitch OAuth use case: build the authorize URL and complete the callback.

The service holds the CSRF state, exchanges the callback code for a token,
fetches the Twitch profile, and persists the user + token through the
UserRepository/TokenStore ports.
"""

import logging
import secrets
import time

from src.application.ports import TokenStore, UserRepository
from src.domain.entities import User
from src.domain.exceptions import OAuthFlowError
from src.providers.token import TwitchAuth

logger = logging.getLogger(__name__)


class OAuthService:
    """Authorize-URL + callback use case for the Twitch OAuth flow."""

    # Pending CSRF states live at most this long before being pruned.
    STATE_TTL = 900.0

    def __init__(
        self,
        auth: TwitchAuth,
        users_repo: UserRepository,
        token_store: TokenStore,
    ) -> None:
        self._auth = auth
        self._users_repo = users_repo
        self._token_store = token_store
        # Pending CSRF states: state -> issued timestamp. Generated per
        # authorize_url() call, consumed once by the matching callback, so
        # concurrent authorizations don't invalidate each other.
        self._states: dict[str, float] = {}

    def authorize_url(self) -> str:
        """Return a Twitch authorize URL with a fresh single-use CSRF state."""
        self._prune_states()
        state = secrets.token_urlsafe(16)
        self._states[state] = time.time()
        return self._auth.generate_code_url(state=state)

    async def complete_authorization(self, code: str, state: str) -> User:
        """Exchange the callback code and store the user + token."""
        issued = self._states.pop(state, None)
        if issued is None or time.time() - issued > self.STATE_TTL:
            raise OAuthFlowError("State mismatch — start over.")
        token = await self._auth.get_token_from_code(code)
        profile = await self._auth.get_user_profile(token.access_token)
        twitch_id = str(profile.get("id") or "")
        if not twitch_id:
            raise OAuthFlowError("Profile missing twitch user id")
        user = await self._users_repo.upsert_twitch_user(
            twitch_id=twitch_id,
            login=str(profile.get("login") or ""),
            display_name=str(
                profile.get("display_name") or profile.get("login") or ""
            ),
            profile_image_url=str(profile.get("profile_image_url") or ""),
        )
        await self._token_store.save(user.id, token)
        logger.info("[%s] authorized via Twitch", user.login)
        return user

    def _prune_states(self) -> None:
        now = time.time()
        for state, issued in list(self._states.items()):
            if now - issued > self.STATE_TTL:
                self._states.pop(state, None)
