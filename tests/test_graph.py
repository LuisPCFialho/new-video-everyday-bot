import httpx
import pytest

from reels_bot.graph import GraphClient, GraphError


def client(responses, sleeps=None):
    """GraphClient cujo transporte devolve `responses` por ordem."""
    calls = []

    def handler(request):
        calls.append(request)
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    sleeps = [] if sleeps is None else sleeps
    c = GraphClient("secret-token", "v23.0", http=httpx.Client(transport=httpx.MockTransport(handler)),
                    sleep=sleeps.append)
    return c, calls


def test_token_sent_in_header_not_url():
    c, calls = client([httpx.Response(200, json={"ok": True})])
    assert c.get("me", fields="id") == {"ok": True}
    assert calls[0].headers["Authorization"] == "Bearer secret-token"
    assert "secret-token" not in str(calls[0].url)


def test_retries_5xx_with_backoff_then_succeeds():
    sleeps = []
    c, calls = client([httpx.Response(500, text="err"), httpx.Response(503, json={"error": {"message": "x"}}),
                       httpx.Response(200, json={"id": "1"})], sleeps)
    assert c.get("x") == {"id": "1"}
    assert len(calls) == 3 and sleeps == [2.0, 4.0]


def test_network_errors_give_up_after_three():
    c, calls = client([httpx.ConnectError("x")] * 3)
    with pytest.raises(GraphError, match="rede") as exc:
        c.get("x")
    assert exc.value.transient and len(calls) == 3


def test_client_error_not_retried():
    c, calls = client([httpx.Response(400, json={"error": {"message": "Invalid parameter", "code": 100,
                                                            "error_subcode": 2207026}})])
    with pytest.raises(GraphError, match="Invalid parameter") as exc:
        c.get("x")
    assert (exc.value.code, exc.value.subcode, exc.value.transient) == (100, 2207026, False)
    assert len(calls) == 1


def test_is_transient_flag_retried():
    c, calls = client([httpx.Response(400, json={"error": {"message": "t", "code": 9007, "is_transient": True}}),
                       httpx.Response(200, json={"id": "ok"})])
    assert c.get("x")["id"] == "ok" and len(calls) == 2


def test_post_without_retry():
    c, calls = client([httpx.Response(500, json={"error": {"message": "x"}})])
    with pytest.raises(GraphError):
        c.post("IG/media_publish", {"creation_id": "1"}, retry=False)
    assert len(calls) == 1


def test_only_media_and_media_publish_posts_allowed():
    c, calls = client([])
    for path in ("123", "IG/comments", "123/media/delete"):
        with pytest.raises(PermissionError):
            c.post(path, {})
    assert calls == []
    assert not hasattr(c, "delete")
