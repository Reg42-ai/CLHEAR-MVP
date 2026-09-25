"""Large answers travel compressed to clients that accept it."""


def test_large_json_is_gzipped_for_clients_that_accept_it(client):
    response = client.get("/api/clhear/layers", headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200 and response.headers["content-encoding"] == "gzip"
    assert response.json()["layers"]
    plain = client.get("/api/clhear/layers", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in plain.headers and plain.json() == response.json()
