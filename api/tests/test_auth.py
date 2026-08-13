def test_health_is_open_regardless_of_api_key(client, monkeypatch):
    monkeypatch.setenv("API_ACCESS_KEY", "secret-key")
    response = client.get("/health")
    assert response.status_code == 200


def test_parse_open_when_key_unset(client, monkeypatch):
    monkeypatch.delenv("API_ACCESS_KEY", raising=False)
    response = client.post("/parse", json={"q": "טויוטה קורולה"})
    assert response.status_code == 200


def test_parse_requires_key_when_configured(client, monkeypatch):
    monkeypatch.setenv("API_ACCESS_KEY", "secret-key")

    missing = client.post("/parse", json={"q": "טויוטה קורולה"})
    assert missing.status_code == 403

    wrong = client.post("/parse", json={"q": "טויוטה קורולה"}, headers={"X-API-Key": "nope"})
    assert wrong.status_code == 403

    ok = client.post("/parse", json={"q": "טויוטה קורולה"}, headers={"X-API-Key": "secret-key"})
    assert ok.status_code == 200


def test_metrics_endpoint_is_open_regardless_of_api_key(client, monkeypatch):
    monkeypatch.setenv("API_ACCESS_KEY", "secret-key")
    response = client.get("/metrics")
    assert response.status_code == 200
