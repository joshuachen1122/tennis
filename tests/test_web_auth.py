from types import SimpleNamespace

from fastapi.testclient import TestClient

from tennisbot.web.app import create_app


class FakeEngine:
    def __init__(self):
        self.db = SimpleNamespace()
        self.stats, self.health, self.last_review, self.matches = {}, {}, None, {}
        self.stream = SimpleNamespace(connected=False)
        self.enabled = True

    def live_view(self):
        return []

    def positions_view(self):
        return {"rows": [], "cost": 0.0, "unreal": 0.0}

    async def pnl_series(self):
        return {"now": 2, "start": 1, "trades": [], "strategies": ["model_edge"]}

    def controls_state(self):
        return {"agent_enabled": self.enabled, "params": {}}

    async def set_agent_enabled(self, enabled):
        self.enabled = enabled


def test_view_is_public_but_controls_need_password():
    eng = FakeEngine()
    c = TestClient(create_app(eng, "secret"))
    assert c.get("/").status_code == 200
    assert c.get("/partials/live").status_code == 200
    assert c.get("/partials/positions").status_code == 200
    assert c.get("/partials/pnl").status_code == 200
    r = c.post("/controls/agent", data={"action": "stop"})
    assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"]
    assert eng.enabled
    assert c.post("/controls/agent", data={"action": "stop"}, auth=("", "wrong")).status_code == 401
    assert c.post("/controls/agent", data={"action": "stop"}, auth=("", "secret")).status_code == 200
    assert not eng.enabled
