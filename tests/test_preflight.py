import pytest
from lib.preflight import check_create_privilege

class FakeCur:
    def __init__(self, result): self._r = result
    def execute(self, *_): pass
    def fetchone(self): return (self._r,)
    def __enter__(self): return self
    def __exit__(self, *a): pass

class FakeConn:
    def __init__(self, result): self._r = result
    def cursor(self): return FakeCur(self._r)

def test_passes_when_has_create():
    check_create_privilege(FakeConn(True), "grafana")  # no raise

def test_raises_actionable_error_when_missing():
    with pytest.raises(PermissionError) as e:
        check_create_privilege(FakeConn(False), "grafana")
    assert "GRANT CREATE ON DATABASE grafana" in str(e.value)
