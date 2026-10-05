from fastapi.testclient import TestClient

from energy_tracker import api

client = TestClient(api.app)


def test_folded_sections_are_saved_and_returned():
    client.post("/api/layout", json={"folded": [], "bill_years": []})
    body = {"folded": ["bills-heading", "tariff-rates"], "bill_years": [2025]}
    assert client.post("/api/layout", json=body).json() == {"saved": True, **body}
    assert client.get("/api/layout").json() == {"saved": True, **body}
    assert client.post("/api/layout", json={"folded": ["x" * 81]}).status_code == 422
