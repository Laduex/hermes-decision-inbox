from fastapi.testclient import TestClient

from service.app.main import create_app


def owner_headers(client: TestClient, settings) -> dict[str, str]:
    auth = client.post(
        "/api/auth/tailscale",
        headers={"Tailscale-User-Login": settings.tailscale_owner_login},
    )
    assert auth.status_code == 200
    return {"Authorization": f"Bearer {auth.json()['token']}"}


def test_publish_auth_and_owner_inbox(settings, single_request):
    with TestClient(create_app(settings)) as client:
        denied = client.post("/internal/v1/decisions", json=single_request)
        assert denied.status_code == 401
        response = client.post(
            "/internal/v1/decisions", json=single_request,
            headers={"Authorization": "Bearer iris-token"},
        )
        assert response.status_code == 200
        decision_id = response.json()["decision_id"]
        assert response.json()["decision_url"] == f"{settings.mini_app_url}?decision={decision_id}"
        assert response.json()["delivery_mode"] == "session_api"

        owner = owner_headers(client, settings)
        inbox = client.get("/api/inbox?tab=new", headers=owner)
        assert inbox.status_code == 200
        assert inbox.json()["items"][0]["decision_id"] == decision_id
        assert inbox.json()["items"][0]["summary"] == single_request["summary"]
        assert inbox.json()["items"][0]["source_profile"] == "iris"


def test_publisher_can_read_open_decisions_for_its_session(settings, single_request):
    with TestClient(create_app(settings)) as client:
        published = client.post(
            "/internal/v1/decisions", json=single_request,
            headers={"Authorization": "Bearer iris-token"},
        ).json()
        response = client.get(
            "/internal/v1/decisions",
            params={"scope": "session", "source_session_id": "session-1"},
            headers={"Authorization": "Bearer iris-token"},
        )
        assert response.status_code == 200
        assert response.json()["items"][0]["decision_id"] == published["decision_id"]
        assert response.json()["items"][0]["cards"][0]["summary"] == single_request["summary"]


def test_publisher_read_is_profile_scoped_and_requires_session_for_session_scope(settings, single_request):
    with TestClient(create_app(settings)) as client:
        client.post(
            "/internal/v1/decisions", json=single_request,
            headers={"Authorization": "Bearer iris-token"},
        )
        missing_session = client.get(
            "/internal/v1/decisions",
            params={"scope": "session"},
            headers={"Authorization": "Bearer iris-token"},
        )
        assert missing_session.status_code == 422
        other_profile = client.get(
            "/internal/v1/decisions",
            params={"scope": "profile"},
            headers={"Authorization": "Bearer default-token"},
        )
        assert other_profile.status_code == 200
        assert other_profile.json()["items"] == []


def test_tailscale_auth_rejects_missing_and_wrong_identity(settings):
    with TestClient(create_app(settings)) as client:
        assert client.post("/api/auth/tailscale").status_code == 401
        assert client.post(
            "/api/auth/tailscale", headers={"Tailscale-User-Login": "intruder@example.com"}
        ).status_code == 401


def test_full_ordinary_api_submission_is_versioned_and_idempotent(settings, single_request):
    with TestClient(create_app(settings)) as client:
        published = client.post(
            "/internal/v1/decisions", json=single_request,
            headers={"Authorization": "Bearer iris-token"},
        ).json()
        owner = owner_headers(client, settings)
        decision = client.get(f"/api/decisions/{published['decision_id']}", headers=owner).json()
        card = decision["cards"][0]
        response = client.put(
            f"/api/cards/{card['card_id']}/response",
            headers=owner,
            json={"card_version": card["version"], "outcome": "recommended", "note": "Proceed"},
        )
        assert response.status_code == 200
        reviewed = response.json()
        submitted = client.post(
            f"/api/decisions/{decision['decision_id']}/submit",
            headers=owner,
            json={"expected_version": reviewed["version"]},
        )
        assert submitted.status_code == 200
        assert submitted.json()["status"] == "QUEUED_FOR_RESUME"
        repeated = client.post(
            f"/api/decisions/{decision['decision_id']}/submit",
            headers=owner,
            json={"expected_version": reviewed["version"]},
        )
        assert repeated.status_code == 409


def test_profile_authenticated_conversation_continuation_lifecycle(settings, single_request):
    request = dict(single_request)
    request.update({
        "source_surface": "discord",
        "source_session_key": "agent:iris:discord:channel:thread",
    })
    with TestClient(create_app(settings)) as client:
        published = client.post(
            "/internal/v1/decisions", json=request,
            headers={"Authorization": "Bearer iris-token"},
        ).json()
        assert published["source_surface"] == "discord"
        assert published["delivery_mode"] == "conversation"
        owner = owner_headers(client, settings)
        decision = client.get(f"/api/decisions/{published['decision_id']}", headers=owner).json()
        reviewed = client.put(
            f"/api/cards/{decision['cards'][0]['card_id']}/response",
            headers=owner,
            json={"card_version": 1, "outcome": "recommended", "note": "Ship it"},
        ).json()
        submitted = client.post(
            f"/api/decisions/{decision['decision_id']}/submit",
            headers=owner,
            json={"expected_version": reviewed["version"]},
        ).json()
        assert submitted["status"] == "QUEUED_FOR_RESUME"

        denied = client.get(
            "/internal/v1/continuations",
            headers={"Authorization": "Bearer default-token"},
        )
        assert denied.json()["items"] == []
        pending = client.get(
            "/internal/v1/continuations",
            headers={"Authorization": "Bearer iris-token"},
        ).json()["items"]
        execution_id = pending[0]["execution_id"]
        claimed = client.post(
            f"/internal/v1/continuations/{execution_id}/claim",
            headers={"Authorization": "Bearer iris-token"},
            json={"consumer_id": "iris-canary", "lease_seconds": 60},
        ).json()
        assert claimed["manifest"]["responses"][0]["selected_option_label"] == "Use staged deployment"
        lease = claimed["lease_token"]
        assert client.post(
            f"/internal/v1/continuations/{execution_id}/dispatch",
            headers={"Authorization": "Bearer iris-token"},
            json={"lease_token": lease},
        ).status_code == 200
        completed = client.post(
            f"/internal/v1/continuations/{execution_id}/complete",
            headers={"Authorization": "Bearer iris-token"},
            json={"lease_token": lease, "result": {"assistant_response": "Deployment finished"}},
        )
        assert completed.status_code == 200
        assert completed.json()["status"] == "COMPLETED"


def test_owner_can_discard_an_open_decision(settings, single_request):
    with TestClient(create_app(settings)) as client:
        published = client.post(
            "/internal/v1/decisions", json=single_request,
            headers={"Authorization": "Bearer iris-token"},
        ).json()
        owner = owner_headers(client, settings)
        decision = client.get(f"/api/decisions/{published['decision_id']}", headers=owner).json()
        response = client.post(
            f"/api/decisions/{decision['decision_id']}/discard",
            headers=owner,
            json={"expected_version": decision["version"]},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "CANCELLED"


def test_owner_can_archive_an_open_decision_and_list_archive(settings, single_request):
    with TestClient(create_app(settings)) as client:
        published = client.post(
            "/internal/v1/decisions", json=single_request,
            headers={"Authorization": "Bearer iris-token"},
        ).json()
        owner = owner_headers(client, settings)
        decision = client.get(f"/api/decisions/{published['decision_id']}", headers=owner).json()
        response = client.post(
            f"/api/decisions/{decision['decision_id']}/archive",
            headers=owner,
            json={"expected_version": decision["version"]},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "ARCHIVED"
        inbox = client.get("/api/inbox?tab=all", headers=owner)
        archive = client.get("/api/inbox?tab=archive", headers=owner)
        assert all(item["decision_id"] != decision["decision_id"] for item in inbox.json()["items"])
        assert archive.json()["items"][0]["decision_id"] == decision["decision_id"]


def test_card_outcome_is_validated_at_api_boundary(settings, single_request):
    with TestClient(create_app(settings)) as client:
        decision_id = client.post(
            "/internal/v1/decisions", json=single_request,
            headers={"Authorization": "Bearer iris-token"},
        ).json()["decision_id"]
        owner = owner_headers(client, settings)
        card = client.get(f"/api/decisions/{decision_id}", headers=owner).json()["cards"][0]
        response = client.put(
            f"/api/cards/{card['card_id']}/response", headers=owner,
            json={"card_version": 1, "outcome": "approve_everything"},
        )
        assert response.status_code == 422
