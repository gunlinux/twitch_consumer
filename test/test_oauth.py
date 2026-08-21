from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.application.oauth import OAuthService
from src.domain.entities import TokenResponse, User
from src.interfaces.dependencies import get_oauth_service
from src.interfaces.routes.oauth import make_router


class FakeAuth:
    def __init__(self) -> None:
        self.exchanged: list[str] = []

    def generate_code_url(self, state: str) -> str:
        return f"https://id.twitch.tv/oauth2/authorize?state={state}"

    async def get_token_from_code(self, code: str) -> TokenResponse:
        self.exchanged.append(code)
        return TokenResponse(
            access_token="at", expires_in=3600, token_type="bearer", refresh_token="rt"
        )

    async def get_user_profile(self, access_token: str) -> dict:
        return {
            "id": "123",
            "login": "chan",
            "display_name": "Chan",
            "profile_image_url": "https://example.com/avatar.png",
        }


class FailingAuth(FakeAuth):
    async def get_token_from_code(self, code: str) -> TokenResponse:
        raise RuntimeError("token endpoint down")


class ScriptNameAuth(FakeAuth):
    async def get_user_profile(self, access_token: str) -> dict:
        return {
            "id": "123",
            "login": "chan",
            "display_name": "<img src=x onerror=alert(1)>",
            "profile_image_url": "",
        }


class FakeUserRepo:
    def __init__(self) -> None:
        self.upserts: list[dict] = []

    async def upsert_twitch_user(self, **kwargs) -> User:
        self.upserts.append(kwargs)
        return User(
            id=1,
            twitch_id=kwargs["twitch_id"],
            login=kwargs["login"],
            display_name=kwargs["display_name"],
            profile_image_url=kwargs["profile_image_url"],
        )


class FakeTokenStore:
    def __init__(self) -> None:
        self.saved: list[tuple[int, TokenResponse]] = []

    async def save(self, user_id: int, token: TokenResponse) -> None:
        self.saved.append((user_id, token))


def make_service(
    auth: FakeAuth | None = None,
) -> tuple[OAuthService, FakeAuth, FakeUserRepo, FakeTokenStore]:
    auth = auth or FakeAuth()
    repo = FakeUserRepo()
    store = FakeTokenStore()
    return OAuthService(auth, repo, store), auth, repo, store


def make_client(
    service: OAuthService, redirect_uri: str = "http://localhost:17563"
) -> TestClient:
    """Test app with the OAuth router and a faked service dependency.

    Created without the ``with`` block so the lifespan (and the database)
    never runs.
    """
    test_app = FastAPI()
    test_app.include_router(make_router(redirect_uri))
    test_app.dependency_overrides[get_oauth_service] = lambda: service
    return TestClient(test_app)


def state_from_redirect(client: TestClient) -> str:
    """GET / and return the CSRF state embedded in the authorize redirect."""
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 302
    query = parse_qs(urlparse(response.headers["location"]).query)
    return query["state"][0]


def test_root_redirects_to_authorize():
    service, _, _, _ = make_service()
    client = make_client(service)

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 302
    state = parse_qs(urlparse(response.headers["location"]).query)["state"][0]
    assert state in service._states


def test_callback_stores_user_and_token():
    service, auth, repo, store = make_service()
    client = make_client(service)
    state = state_from_redirect(client)

    response = client.get(f"/?code=c1&state={state}")

    assert response.status_code == 200
    assert "Chan" in response.text
    assert auth.exchanged == ["c1"]
    assert repo.upserts == [
        {
            "twitch_id": "123",
            "login": "chan",
            "display_name": "Chan",
            "profile_image_url": "https://example.com/avatar.png",
        }
    ]
    assert len(store.saved) == 1
    assert store.saved[0][0] == 1
    assert store.saved[0][1].access_token == "at"


def test_callback_rejects_state_mismatch():
    service, _, repo, store = make_service()
    client = make_client(service)

    response = client.get("/?code=c1&state=wrong")

    assert response.status_code == 400
    assert "State mismatch" in response.text
    assert repo.upserts == []
    assert store.saved == []


def test_callback_reports_twitch_error():
    service, _, _, _ = make_service()
    client = make_client(service)

    response = client.get("/?error=access_denied")

    assert response.status_code == 400
    assert "access_denied" in response.text


def test_callback_returns_error_page_on_failure():
    service, _, _, _ = make_service(auth=FailingAuth())
    client = make_client(service)
    state = state_from_redirect(client)

    response = client.get(f"/?code=c1&state={state}")

    assert response.status_code == 400
    assert "check the server log" in response.text
    assert "token endpoint down" not in response.text


def test_router_serves_redirect_uri_path():
    service, _, _, _ = make_service()

    local = make_client(service, "http://localhost:17563")
    assert local.get("/", follow_redirects=False).status_code == 302

    hosted = make_client(service, "https://x.example.com/callback")
    assert hosted.get("/callback", follow_redirects=False).status_code == 302


def test_error_page_escapes_html():
    service, _, _, _ = make_service()
    client = make_client(service)

    response = client.get("/?error=%3Cscript%3Ealert(1)%3C/script%3E")

    assert response.status_code == 400
    assert "&lt;script&gt;" in response.text
    assert "<script>" not in response.text


def test_success_page_escapes_display_name():
    service, _, _, _ = make_service(auth=ScriptNameAuth())
    client = make_client(service)
    state = state_from_redirect(client)

    response = client.get(f"/?code=c1&state={state}")

    assert response.status_code == 200
    assert "&lt;img src=x onerror=alert(1)&gt;" in response.text
    assert "<img" not in response.text


def test_state_is_single_use():
    service, _, _, store = make_service()
    client = make_client(service)
    state = state_from_redirect(client)

    first = client.get(f"/?code=c1&state={state}")
    assert first.status_code == 200

    second = client.get(f"/?code=c1&state={state}")
    assert second.status_code == 400
    assert "State mismatch" in second.text
    assert len(store.saved) == 1


def test_multiple_pending_states_allowed():
    service, _, _, _ = make_service()
    client = make_client(service)
    state1 = state_from_redirect(client)
    state2 = state_from_redirect(client)

    assert state1 != state2
    assert client.get(f"/?code=c1&state={state1}").status_code == 200
    assert client.get(f"/?code=c1&state={state2}").status_code == 200
