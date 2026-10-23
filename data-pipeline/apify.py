"""Minimal local stand-in for the Apify SDK so the Actor's scoring code runs as a plain script (no Apify platform, $0)."""
import sys

class _Log:
    def _p(self, lvl, m): print(f"[{lvl}] {m}", file=sys.stderr, flush=True)
    def info(self, m, *a, **k): self._p("INFO", m)
    def warning(self, m, *a, **k): self._p("WARN", m)
    def error(self, m, *a, **k): self._p("ERROR", m)
    def debug(self, m, *a, **k): pass
    exception = error

class _Actor:
    log = _Log()
    def __init__(self): self.input, self.items, self.kv, self.failed = {}, [], {}, None
    async def __aenter__(self): return self
    async def __aexit__(self, *exc): return False
    async def get_input(self): return dict(self.input)
    async def push_data(self, item, charged_event_name=None): self.items.append(item); return None
    async def set_value(self, key, value, **k): self.kv[key] = value
    async def set_status_message(self, msg, **k): self.log.info(f"STATUS {msg}")
    async def fail(self, status_message=None, **k): self.failed = status_message or "failed"
    async def open_key_value_store(self, **k): raise RuntimeError("no key-value store outside Apify")

Actor = _Actor()
