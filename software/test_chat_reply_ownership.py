"""Reply-box ownership invariants. Test real dispatch in test_request_reply_ownership."""

from source.server import server


def test_expired_reply_owner_cannot_write_to_another_box():
    state = {}
    expired = server._request_owner(server._new_reply_token(state))
    active = server._request_owner(server._new_reply_token(state))
    server._close_reply_box(state, expired.reply_token)
    server._install_ownership(state, active)

    assert not server._post_reply(state, "stale", ownership=expired)
    assert state["reply_waiters"][active.reply_token].empty()
    assert server._post_reply(state, "current", ownership=active)
    assert state["reply_waiters"][active.reply_token].get_nowait() == "current"


def test_stale_cleanup_cannot_clear_a_new_owner():
    state = {}
    old = server._request_owner()
    new = server._request_owner()
    server._install_ownership(state, new)

    assert not server._clear_ownership(state, expected=old)
    assert server._ownership(state) is new
    assert server._clear_ownership(state, expected=new)
    assert server._ownership(state) is server.LOCAL_OWNERSHIP
