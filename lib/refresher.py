from __future__ import annotations
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger("refresher")

@dataclass
class RefreshState:
    last_good_token: str

def refresh_once(state: RefreshState,
                 mint: Callable[[], str],
                 write_server_cred: Callable[[str], None],
                 reload: Callable[[], None]) -> None:
    try:
        token = mint()
    except Exception:
        log.exception("token mint failed; keeping last-good token")
        return
    # write_server_cred persists the token to the ini AND records it as
    # state.last_good_token, both under the ini lock (see startup._write_server_cred).
    # We deliberately do NOT set state.last_good_token here — doing so outside the
    # lock would let a concurrent PgBouncer relaunch read a stale token and
    # overwrite the freshly-written one on disk.
    write_server_cred(token)
    reload()

def run_loop(state: RefreshState,
             mint: Callable[[], str],
             write_server_cred: Callable[[str], None],
             reload: Callable[[], None],
             interval_s: int, stop: threading.Event) -> None:
    while not stop.wait(interval_s):
        try:
            refresh_once(state, mint, write_server_cred, reload)
        except Exception:
            log.exception("refresh cycle error; will retry next interval")
