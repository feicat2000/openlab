"""
Tests for ProtocolGenerator (all API calls mocked).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.nlp.protocol_generator import ProtocolGenerator, GenerationResult


# ── Helpers ───────────────────────────────────────────────────────────────────

VALID_YAML = """
metadata:
  name: "Test Protocol"
  version: "1.0"
  author: "AI"
  safety_level: BSL1

materials:
  reagents:
    - name: Water
      volume_ul: 500
      hazard_class: none

steps:
  - id: 1
    name: Wait
    type: wait
    params:
      duration_s: 5
""".strip()

YAML_IN_FENCE = f"```yaml\n{VALID_YAML}\n```"


def _make_mock_response(text: str, input_tokens: int = 100, output_tokens: int = 200):
    resp = MagicMock()
    resp.content = [MagicMock(text=text)]
    resp.usage.input_tokens = input_tokens
    resp.usage.output_tokens = output_tokens
    return resp


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestNoApiKey:
    def test_generate_without_key_returns_failure(self):
        gen = ProtocolGenerator(api_key=None)
        result = gen.generate("Do something")
        assert result.success is False
        assert result.error is not None
        assert "API key" in result.error


class TestParseYamlFromResponse:
    def test_extracts_from_fenced_block(self):
        gen = ProtocolGenerator()
        yaml_str = gen._parse_yaml_from_response(YAML_IN_FENCE)
        assert "metadata" in yaml_str
        assert "steps" in yaml_str

    def test_extracts_from_plain_yaml_fence(self):
        gen = ProtocolGenerator()
        text = f"Here is your protocol:\n```\n{VALID_YAML}\n```"
        yaml_str = gen._parse_yaml_from_response(text)
        assert "metadata" in yaml_str

    def test_fallback_to_raw_text(self):
        gen = ProtocolGenerator()
        result = gen._parse_yaml_from_response("some text without fence")
        assert result == "some text without fence"


class TestValidate:
    def test_accepts_valid_yaml(self):
        gen = ProtocolGenerator()
        ok, warnings = gen._validate(VALID_YAML)
        assert ok is True

    def test_rejects_empty_string(self):
        gen = ProtocolGenerator()
        ok, warnings = gen._validate("")
        assert ok is False

    def test_rejects_malformed_yaml(self):
        gen = ProtocolGenerator()
        ok, warnings = gen._validate("key: [unclosed")
        assert ok is False

    def test_rejects_missing_steps(self):
        gen = ProtocolGenerator()
        ok, warnings = gen._validate("metadata:\n  name: Test\n")
        assert ok is False

    def test_warns_on_missing_metadata(self):
        gen = ProtocolGenerator()
        yaml_no_meta = "steps:\n  - id: 1\n    name: W\n    type: wait\n    params:\n      duration_s: 1\n"
        ok, warnings = gen._validate(yaml_no_meta)
        assert ok is True
        assert any("metadata" in w for w in warnings)


class TestBuildPrompt:
    def test_includes_description(self):
        gen = ProtocolGenerator()
        p = gen._build_prompt("extract DNA", None, "BSL1")
        assert "extract DNA" in p

    def test_includes_equipment_list(self):
        gen = ProtocolGenerator()
        p = gen._build_prompt("test", ["vortex", "centrifuge"], "BSL2")
        assert "vortex" in p
        assert "centrifuge" in p

    def test_includes_safety_level(self):
        gen = ProtocolGenerator()
        p = gen._build_prompt("test", None, "BSL2")
        assert "BSL2" in p


class TestGenerate:
    def test_generate_success(self):
        gen = ProtocolGenerator(api_key="test-key")
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _make_mock_response(YAML_IN_FENCE)

        with patch("anthropic.Anthropic", return_value=mock_client):
            with patch.object(gen, "_validate", return_value=(True, [])):
                with patch("src.core.protocol_parser.ProtocolParser.load_string") as mock_load:
                    mock_doc = MagicMock()
                    mock_load.return_value = mock_doc
                    result = gen.generate("extract DNA")

        assert result.success is True
        assert result.protocol is mock_doc
        assert result.tokens_used == 300

    def test_generate_api_error_returns_failure(self):
        gen = ProtocolGenerator(api_key="test-key")
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = Exception("API error")

        with patch("anthropic.Anthropic", return_value=mock_client):
            result = gen.generate("test")

        assert result.success is False
        assert "API error" in result.error

    def test_generate_invalid_yaml_returns_failure(self):
        gen = ProtocolGenerator(api_key="test-key")
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _make_mock_response("```yaml\nBAD YAML: [{}\n```")

        with patch("anthropic.Anthropic", return_value=mock_client):
            result = gen.generate("test")

        assert result.success is False


class TestRefine:
    def test_refine_calls_api_with_existing_protocol(self):
        gen = ProtocolGenerator(api_key="test-key")
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _make_mock_response(YAML_IN_FENCE)

        mock_doc = MagicMock()
        mock_doc.metadata.name = "Original"
        mock_doc.metadata.version = "1.0"
        mock_doc.metadata.author = "Alice"
        mock_doc.metadata.safety_level = "BSL1"
        mock_doc.steps = []

        with patch("anthropic.Anthropic", return_value=mock_client):
            with patch.object(gen, "_validate", return_value=(True, [])):
                with patch("src.core.protocol_parser.ProtocolParser.load_string") as mock_load:
                    mock_load.return_value = MagicMock()
                    result = gen.refine(mock_doc, "add a vortex step")

        mock_client.messages.create.assert_called_once()
        call_args = mock_client.messages.create.call_args
        prompt_content = call_args[1]["messages"][0]["content"]
        assert "add a vortex step" in prompt_content
