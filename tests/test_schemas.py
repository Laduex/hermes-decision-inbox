import pytest
from pydantic import ValidationError

from hermes_decision_inbox.schemas import DecisionRequest


def test_single_decision_requires_complete_fields(single_request):
    single_request.pop("details")
    with pytest.raises(ValidationError):
        DecisionRequest.model_validate({k: v for k, v in single_request.items() if not k.startswith("source_") and k != "plugin_version"})


def test_option_ids_must_be_unique(single_request):
    payload = {k: v for k, v in single_request.items() if not k.startswith("source_") and k != "plugin_version"}
    payload["alternatives"][0]["option_id"] = "staged"
    with pytest.raises(ValidationError):
        DecisionRequest.model_validate(payload)


def test_unsupported_execution_kind_is_rejected(single_request):
    payload = {k: v for k, v in single_request.items() if not k.startswith("source_") and k != "plugin_version"}
    payload["execution"] = {
        "execution_kind": "shell_v1", "target_path": "index.md", "operation": "append",
        "proposed_content": "unsafe",
    }
    with pytest.raises(ValidationError):
        DecisionRequest.model_validate(payload)

