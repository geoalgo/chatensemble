import httpx
import respx

from chatensemble.config import AccountConfig
from chatensemble.filters import FetchFilter
from chatensemble.models import ChannelKind
from chatensemble.providers.mattermost import MattermostClient

BASE = "https://mm.test/api/v4"


def _account():
    return AccountConfig(
        name="work",
        type="mattermost",
        settings={"server": "https://mm.test", "username": "me@x", "password": "pw"},
    )


def _mock_common(router):
    router.post(f"{BASE}/users/login").mock(
        return_value=httpx.Response(200, headers={"Token": "sess"}, json={"id": "me"})
    )
    router.get(f"{BASE}/users/me").mock(
        return_value=httpx.Response(200, json={"id": "me", "username": "me"})
    )
    router.get(f"{BASE}/users/me/teams").mock(
        return_value=httpx.Response(200, json=[{"id": "t1", "name": "team1"}])
    )
    router.get(f"{BASE}/users/me/teams/t1/channels/members").mock(
        return_value=httpx.Response(
            200,
            json=[{"channel_id": "c1", "msg_count": 1, "mention_count": 1, "last_viewed_at": 1500}],
        )
    )
    router.get(f"{BASE}/users/me/teams/t1/channels").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": "c1",
                    "name": "general",
                    "display_name": "General",
                    "type": "O",
                    "team_id": "t1",
                    "total_msg_count": 3,
                }
            ],
        )
    )
    router.post(f"{BASE}/users/ids").mock(
        return_value=httpx.Response(200, json=[{"id": "u1", "username": "alice"}])
    )


@respx.mock
def test_authenticate_and_list_channels():
    router = respx.mock
    _mock_common(router)
    c = MattermostClient(_account())
    c.authenticate()
    assert c.http.headers["Authorization"] == "Bearer sess"

    channels = c.list_channels()
    assert len(channels) == 1
    ch = channels[0]
    assert ch.name == "general"
    assert ch.kind == ChannelKind.PUBLIC
    assert ch.unread_count == 2          # total 3 - seen 1
    assert ch.mention_count == 1
    c.close()


@respx.mock
def test_fetch_applies_since_and_unread_flags():
    router = respx.mock
    _mock_common(router)
    router.get(f"{BASE}/channels/c1/posts").mock(
        return_value=httpx.Response(
            200,
            json={
                "order": ["p3", "p2", "p1"],
                "posts": {
                    "p3": {"id": "p3", "message": "newest", "create_at": 3000,
                           "user_id": "u1", "root_id": ""},
                    "p2": {"id": "p2", "message": "mid @me", "create_at": 2000,
                           "user_id": "u1", "root_id": ""},
                    "p1": {"id": "p1", "message": "old", "create_at": 500,
                           "user_id": "me", "root_id": ""},
                },
                "prev_post_id": "",
            },
        )
    )
    c = MattermostClient(_account())
    flt = FetchFilter(since="1970-01-01T00:00:01Z")  # 1000 ms -> drops p1 (500ms)
    msgs = c.fetch(flt)
    ids = [m.id for m in msgs]
    assert ids == ["p2", "p3"]                       # sorted oldest->newest, p1 excluded
    by_id = {m.id: m for m in msgs}
    assert by_id["p3"].is_unread is True             # 3000 > last_viewed 1500, not own
    assert by_id["p2"].is_unread is True             # 2000 > 1500, not own
    assert by_id["p2"].author_name == "alice"
    assert by_id["p2"].is_mention is True            # "@me" in text
    assert by_id["p3"].permalink == "https://mm.test/team1/pl/p3"
    c.close()


@respx.mock
def test_mark_read_posts_channel_view():
    router = respx.mock
    _mock_common(router)
    view = router.post(f"{BASE}/channels/members/me/view").mock(
        return_value=httpx.Response(200, json={"status": "OK"})
    )
    c = MattermostClient(_account())
    c.mark_read("c1")
    assert view.called
    import json

    assert json.loads(view.calls.last.request.content) == {"channel_id": "c1"}
    c.close()
