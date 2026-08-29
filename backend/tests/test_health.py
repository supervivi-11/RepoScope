from fastapi.testclient import TestClient

from app.main import app


def test_health_returns_service_status() -> None:
    """Breaks if the public health contract is missing or changes."""
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "reposcope-api"}
