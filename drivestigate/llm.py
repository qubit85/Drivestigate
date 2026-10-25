"""Local-only Ollama client with bounded responses and validated decisions."""

import json
import re
import time
import urllib.error
import urllib.request
from copy import deepcopy

from .config import SAFETY_PROMPT, validate_settings

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "reason": {"type": "string"},
        "priority": {"type": "integer", "enum": [1, 2, 3]},
        "needs_more_samples": {"type": "boolean"},
        "next_action": {"type": "string"},
        "file_ids": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["summary", "reason", "priority", "needs_more_samples", "next_action", "file_ids"],
    "additionalProperties": False,
}
FILE_SCHEMA = {
    "type": "object",
    "properties": {
        k: v for k, v in SCHEMA["properties"].items() if k in ("summary", "reason", "priority", "file_ids")
    },
    "required": ["summary", "reason", "priority", "file_ids", "topics"],
    "additionalProperties": False,
}
FILE_SCHEMA["properties"]["topics"] = {"type": "array", "items": {"type": "string"}, "maxItems": 5}
REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        k: v for k, v in SCHEMA["properties"].items() if k in ("summary", "reason", "priority", "file_ids")
    },
    "required": ["summary", "reason", "priority", "file_ids", "next_steps"],
    "additionalProperties": False,
}
REPORT_SCHEMA["properties"]["next_steps"] = {"type": "array", "items": {"type": "string"}, "maxItems": 5}


class OllamaError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class Ollama:
    def __init__(self, settings):
        self.settings = validate_settings(settings)
        self.structured = True

        # Never inherit an HTTP proxy for local file excerpts; never follow redirects.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                raise ValueError("Ollama redirects are not allowed.")

        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, path, payload=None, timeout=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            self.settings["ollama_url"] + path, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with self.opener.open(
                req, timeout=timeout or self.settings["request_timeout_seconds"]
            ) as response:
                body = response.read(2 * 1024 * 1024 + 1)
                if len(body) > 2 * 1024 * 1024:
                    raise ValueError("Ollama response exceeded the size limit.")
                return json.loads(body)
        except urllib.error.HTTPError as exc:
            detail = exc.read(1500).decode("utf-8", errors="replace")
            raise OllamaError(exc.code, f"Ollama HTTP {exc.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ValueError(f"Ollama unavailable or request timed out: {exc}") from None

    def models(self):
        return [
            m
            for m in self.request("/api/tags", timeout=5).get("models", [])
            if not m.get("remote_model")
            and not m.get("remote_host")
            and ":cloud" not in m.get("name", "")
            and m.get("size", 0) > 1024 * 1024
        ]

    def test(self):
        names = [m["name"] for m in self.models()]
        if self.settings["model"] not in names:
            raise ValueError("The selected model is not installed in Ollama.")
        info = self.request("/api/show", {"model": self.settings["model"]}, timeout=15)
        if (
            info.get("remote_model")
            or info.get("remote_host")
            or not info.get("model_info")
            or "cloud" in str(info.get("details", {}).get("format", "")).lower()
        ):
            raise ValueError("Only locally stored models may process file contents.")
        return {"ok": True, "model": self.settings["model"], "capabilities": info.get("capabilities", [])}

    def analyze(self, evidence, timeout=None):
        return self._analyze(evidence, SCHEMA, timeout)

    def analyze_file(self, evidence, timeout=None):
        result = self._analyze(evidence, FILE_SCHEMA, timeout)
        topics = result.get("topics")
        if (
            not isinstance(topics, list)
            or len(topics) > 5
            or any(not isinstance(t, str) or not t.strip() or len(t) > 60 for t in topics)
        ):
            raise ValueError("Invalid suggested topics in file assessment.")
        result["topics"] = list(dict.fromkeys(t.strip() for t in topics))
        return result

    def summarize_report(self, evidence, timeout=None):
        result = self._analyze(evidence, REPORT_SCHEMA, timeout)
        steps = result.get("next_steps")
        if (
            not isinstance(steps, list)
            or len(steps) > 5
            or any(not isinstance(s, str) or not s.strip() or len(s) > 600 for s in steps)
        ):
            raise ValueError("Invalid next steps in comprehensive report.")
        # Do not let a narrative confuse folder, sample and file counts. Numeric catalog
        # totals are rendered separately. Dates/identifiers explicitly in assessed text remain valid.
        supported = set(
            re.findall(
                r"\b\d+(?:[.,]\d+)?\b",
                " ".join(
                    str(s.get("id", "")) + " " + str(s.get("rel", "")) + " " + str(s.get("text", ""))
                    for s in evidence.get("samples", [])
                )
                + " "
                + " ".join(str(a.get("folder", "")) for a in evidence.get("areas", [])),
            )
        )
        prose = result["summary"] + " " + result["reason"] + " " + " ".join(steps)
        if set(re.findall(r"\b\d+(?:[.,]\d+)?\b", prose)) - supported:
            raise ValueError(
                "Report narrative introduced unsupported numerical claims; catalog summary retained."
            )
        return result

    def _analyze(self, evidence, schema, timeout=None):
        evidence = deepcopy(evidence)
        images = []
        for sample in evidence.get("samples", []):
            if "image" in sample:
                sample["image_index"] = len(images)
                images.append(sample.pop("image"))
        message = {"role": "user", "content": json.dumps(evidence, ensure_ascii=False)}
        if images:
            message["images"] = images
        payload = {
            "model": self.settings["model"],
            "stream": False,
            "think": False,
            "messages": [
                {
                    "role": "system",
                    "content": SAFETY_PROMPT
                    + "\n\n"
                    + (
                        "Create a concise English comprehensive report from this saved investigation. "
                        "Write two or three connected paragraphs in summary, covering main content, promising areas and coverage gaps. "
                        "Synthesize the supplied assessed examples and aggregate inventory; do not list every folder. "
                        "Leave numerical counts, sizes and example IDs to the charts; do not repeat them in prose. "
                        "Folder assessments do not establish how many individual files were content-reviewed. "
                        "Do not infer contents of unsampled files, turn claims inside files into facts, or invent paths. "
                        "Reason must explain uncertainty and evidence limits. Return up to five practical read-only next_steps "
                        "available in Drivestigate: review evidence paths, resume remaining folders, choose all-file mode, or inspect issues. "
                        "Do not suggest forensic recovery or shell commands. "
                        "file_ids must refer only to the supplied assessed examples. Do not recommend deletion."
                        if schema is REPORT_SCHEMA
                        else self.settings["system_prompt"]
                    )
                    + "\n\nOwner's search focus:\n"
                    + self.settings["search_focus"]
                    + (
                        "\n\nThis is a SINGLE-FILE assessment. Describe only this file and its supplied evidence. "
                        "Text mentioning other files is a claim in this file, not proof those files exist. "
                        "Use wording such as 'The excerpt mentions...' for such claims. Do not describe a "
                        "whole folder or drive as inspected. Topics must describe evidenced content."
                        if schema is FILE_SCHEMA
                        else ""
                    )
                    + "\n\nJSON schema (all fields required; file_ids must be "
                    "integers from the supplied samples). Topics must be short, evidence-based labels, "
                    "not invented entities or instructions:\n" + json.dumps(schema),
                },
                message,
            ],
            "options": {
                "temperature": 0,
                "num_ctx": 16384,
                "num_predict": 1500 if schema is REPORT_SCHEMA else 700,
            },
            "keep_alive": "10m",
        }
        # UTF-8 bytes are a conservative upper bound on ordinary text token counts.
        # Reserve space within the 16k context instead of allowing backend truncation.
        system_bytes = len(payload["messages"][0]["content"].encode("utf-8"))
        if system_bytes > 9000:
            raise ValueError("System prompt and search focus are too long; shorten them for the 16k context.")
        for excerpt_limit in (2200, 800, 200, 0):
            for sample in evidence.get("samples", []):
                sample["text"] = sample.get("text", "")[:excerpt_limit]
            message["content"] = json.dumps(evidence, ensure_ascii=False)
            if system_bytes + len(message["content"].encode("utf-8")) <= 12000:
                break
        else:
            raise ValueError("Sample metadata exceeds the context budget. Reduce samples per round.")
        if self.structured:
            payload["format"] = schema
        started = time.monotonic()
        try:
            response = self.request("/api/chat", payload, timeout=timeout)
        except OllamaError as exc:
            if exc.code != 501 or "structured output" not in str(exc):
                raise
            # MLX currently rejects grammar constraints; validate the identical schema ourselves.
            self.structured = False
            payload.pop("format", None)
            remaining = max(
                0.1, (timeout or self.settings["request_timeout_seconds"]) - (time.monotonic() - started)
            )
            response = self.request("/api/chat", payload, timeout=remaining)
        try:
            content = response["message"]["content"].strip()
            if content.startswith("```json") and content.endswith("```"):
                content = content[7:-3].strip()
            result = json.loads(content)
        except (KeyError, TypeError, json.JSONDecodeError):
            raise ValueError("The model did not return a valid structured response.") from None
        if not isinstance(result, dict):
            raise ValueError("Invalid AI assessment.")
        if set(result) != set(schema["required"]):
            raise ValueError("AI assessment does not match the expected schema.")
        for key in ("summary", "reason", "next_action") if schema is SCHEMA else ("summary", "reason"):
            if not isinstance(result.get(key), str) or len(result[key]) > 3000:
                raise ValueError("Incomplete AI assessment.")
        if type(result.get("priority")) is not int or result["priority"] not in (1, 2, 3):
            raise ValueError("Invalid AI priority.")
        if schema is SCHEMA and type(result.get("needs_more_samples")) is not bool:
            raise ValueError("Invalid sampling decision.")
        ids = result.get("file_ids")
        allowed = {s["id"] for s in evidence.get("samples", [])}
        if not isinstance(ids, list) or any(type(i) is not int or i not in allowed for i in ids):
            raise ValueError("The AI returned an unsupported file reference.")
        return result
