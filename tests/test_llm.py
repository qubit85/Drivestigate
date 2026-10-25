import json

import pytest

from drivestigate.config import DEFAULTS, validate_settings
from drivestigate.llm import Ollama, OllamaError


def valid_result():
    return {
        "summary": "Personal project",
        "reason": "README sample",
        "priority": 2,
        "needs_more_samples": False,
        "next_action": "Review it",
        "file_ids": [1],
    }


def test_mlx_fallback_validates_json_and_does_not_repeat_unsupported_format():
    client = Ollama(DEFAULTS)
    calls = []

    def request(path, payload=None, timeout=None):
        calls.append(dict(payload))
        if "format" in payload:
            raise OllamaError(501, "structured output is unavailable")
        return {"message": {"content": "```json\n" + json.dumps(valid_result()) + "\n```"}}

    client.request = request
    assert client.analyze({"samples": [{"id": 1, "text": "README"}]})["priority"] == 2
    client.analyze({"samples": [{"id": 1}]})
    assert len(calls) == 3
    assert "format" in calls[0] and "format" not in calls[1] and "format" not in calls[2]


def test_cloud_models_are_filtered_and_remote_aliases_rejected():
    client = Ollama(DEFAULTS)
    tags = {
        "models": [
            {"name": DEFAULTS["model"], "size": 7000000000},
            {"name": "big:cloud", "size": 5000000000},
            {"name": "remote", "size": 900000000, "remote_host": "https://ollama.com"},
        ]
    }
    client.request = lambda path, *a, **kw: (
        tags
        if path == "/api/tags"
        else {"remote_model": "hidden-remote", "model_info": {"general.architecture": "qwen"}}
    )
    assert len(client.models()) == 1
    with pytest.raises(ValueError, match="locally stored"):
        client.test()


@pytest.mark.parametrize("response", ["not json", "[]", "null", '{"priority":true}'])
def test_malformed_model_output_cannot_become_a_finding(response):
    client = Ollama(DEFAULTS)
    client.request = lambda *a, **kw: {"message": {"content": response}}
    with pytest.raises(ValueError):
        client.analyze({"samples": [{"id": 1}]})


@pytest.mark.parametrize(
    "field,value",
    [("ollama_url", []), ("depth", {}), ("max_folders", True), ("system_prompt", 1), ("profiles", "bad")],
)
def test_malformed_settings_produce_validation_errors(field, value):
    with pytest.raises(ValueError):
        validate_settings({field: value})
