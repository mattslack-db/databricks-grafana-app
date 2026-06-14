from lib.refresher import refresh_once, RefreshState

def test_refresh_once_mints_writes_and_reloads():
    events = []
    state = RefreshState(last_good_token="old")
    def mint(): return "new-token"
    def write_server_cred(tok): events.append(("write", tok))
    def reload(): events.append(("reload",))
    refresh_once(state, mint, write_server_cred, reload)
    assert state.last_good_token == "new-token"
    assert events == [("write", "new-token"), ("reload",)]

def test_refresh_once_keeps_last_good_on_mint_failure():
    state = RefreshState(last_good_token="old")
    def mint(): raise RuntimeError("boom")
    def write_server_cred(tok): raise AssertionError("should not write")
    def reload(): raise AssertionError("should not reload")
    refresh_once(state, mint, write_server_cred, reload)  # must not raise
    assert state.last_good_token == "old"
