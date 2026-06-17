import threading

from lib.refresher import refresh_once, run_loop, RefreshState

def test_refresh_once_mints_writes_and_reloads():
    events = []
    state = RefreshState(last_good_token="old")
    def mint(): return "new-token"
    # In production, write_server_cred records last_good_token under the ini
    # lock; emulate that contract here so the test mirrors real behavior.
    def write_server_cred(tok):
        events.append(("write", tok)); state.last_good_token = tok
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

def test_refresh_once_does_not_set_last_good_token_itself():
    # refresh_once must NOT touch state.last_good_token directly — only the
    # (locked) write_server_cred callback may, to avoid the relaunch race.
    state = RefreshState(last_good_token="old")
    def mint(): return "new-token"
    def write_server_cred(tok): pass  # callback chooses NOT to record
    def reload(): pass
    refresh_once(state, mint, write_server_cred, reload)
    assert state.last_good_token == "old"

def test_run_loop_exits_immediately_when_stop_already_set():
    # stop.wait(interval) returns True right away when the event is set, so the
    # loop body never runs (mint must not be called).
    state = RefreshState(last_good_token="old")
    stop = threading.Event(); stop.set()
    def mint(): raise AssertionError("loop body should not run when stopped")
    run_loop(state, mint, lambda t: None, lambda: None, interval_s=0, stop=stop)

def test_run_loop_absorbs_cycle_errors_and_continues():
    # A failing cycle must be logged and swallowed (retry next interval), not
    # propagated out of run_loop. Stop after the first cycle.
    state = RefreshState(last_good_token="old")
    stop = threading.Event()
    calls = {"n": 0}
    def mint():
        calls["n"] += 1
        stop.set()  # ensure the loop ends after this cycle
        raise RuntimeError("transient")
    # Must return normally despite the raised error inside the cycle.
    run_loop(state, mint, lambda t: None, lambda: None, interval_s=0, stop=stop)
    assert calls["n"] == 1
