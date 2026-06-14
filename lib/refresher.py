from __future__ import annotations
import logging, threading, time
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
    reload()
    state.last_good_token = token

def run_loop(state: RefreshState, mint, write_server_cred, reload,
             interval_s: int, stop: threading.Event) -> None:
    while not stop.wait(interval_s):
        try:
            refresh_once(state, mint, write_server_cred, reload)
        except Exception:
            log.exception("refresh cycle error; will retry next interval")
