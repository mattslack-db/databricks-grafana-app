from __future__ import annotations
import logging
import threading
from dataclasses import dataclass

log = logging.getLogger("refresher")

@dataclass
class RefreshState:
    last_good_token: str

def refresh_once(state: RefreshState, mint, write_server_cred, reload) -> None:
    try:
        token = mint()
    except Exception:
        log.exception("token mint failed; keeping last-good token")
        return
    write_server_cred(token)
    # Track the on-disk state: PgBouncer reads the ini, so last_good_token must
    # reflect the disk write — not the reload's success. A relaunch after a
    # failed reload still needs the token that is actually on disk.
    state.last_good_token = token
    reload()

def run_loop(state: RefreshState, mint, write_server_cred, reload,
             interval_s: int, stop: threading.Event) -> None:
    while not stop.wait(interval_s):
        try:
            refresh_once(state, mint, write_server_cred, reload)
        except Exception:
            log.exception("refresh cycle error; will retry next interval")
