"""
Protocol Generator – uses the Claude API (Anthropic) to generate OpenLab
protocol YAML from a natural-language description.

Falls back gracefully if the API key is not set or the API is unreachable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import yaml
from loguru import logger

from src.core.protocol_parser import ProtocolParser


# ── Result model ──────────────────────────────────────────────────────────────

@dataclass
class GenerationResult:
    success: bool
    protocol: Optional[object] = None   # ProtocolDocument or None
    yaml_string: str = ""
    tokens_used: int = 0
    model: str = ""
    warnings: List[str] = field(default_factory=list)
    error: Optional[str] = None


# ── System prompt template ────────────────────────────────────────────────────

_SYSTEM_PROMPT = """You are an expert laboratory automation engineer for the OpenLab platform.
Your task is to generate valid OpenLab protocol YAML files from a natural-language description.

Available step types:
  pipette, vortex, centrifuge, incubate, magnetic_sep, rock,
  tighten_cap, loosen_cap, invert_tube, transfer_tube,
  temperature_set, wait, comment, manual

Protocol YAML structure:
```yaml
metadata:
  name: "Protocol Name"
  version: "1.0"
  author: "AI Generated"
  description: "..."
  safety_level: BSL1  # BSL1 | BSL2 | BSL3 | CHEM

materials:
  reagents:
    - name: "Reagent Name"
      volume_ul: 500
      hazard_class: none  # none | flammable | toxic | corrosive | biohazard

consumables:
  - name: "1.5mL Eppendorf tubes"
    quantity: 8

equipment:
  - name: opentrons
  - name: vortex  # only include equipment actually used

steps:
  - id: 1
    name: "Step name"
    type: vortex
    params:
      rpm: 2000
      duration_s: 30
    notes: "Optional note"

safety_notes:
  - "Relevant safety information"
```

RULES:
1. Every step must have a unique integer id starting at 1
2. Only include equipment that is used in the steps
3. Match the safety_level to the hazard level of the reagents
4. Be concise but complete
5. Output ONLY the YAML, enclosed in ```yaml ... ``` code fences
"""


# ── ProtocolGenerator ─────────────────────────────────────────────────────────

class ProtocolGenerator:
    """
    Generate OpenLab protocol YAML documents via the Claude API.

    Usage::

        gen = ProtocolGenerator(api_key="sk-ant-...")
        result = gen.generate("Extract DNA from 8 blood samples using magnetic beads")
        if result.success:
            print(result.yaml_string)
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "claude-opus-4-6",
    ) -> None:
        self._api_key = api_key
        self._model = model

    # ── Public API ────────────────────────────────────────────────────────────

    def generate(
        self,
        description: str,
        equipment_available: Optional[List[str]] = None,
        safety_level: str = "BSL1",
        max_steps: int = 30,
    ) -> GenerationResult:
        """Generate a protocol from a natural-language description."""
        if not self._api_key:
            return GenerationResult(
                success=False,
                error="No API key provided. Set ANTHROPIC_API_KEY or pass api_key to ProtocolGenerator.",
            )

        try:
            import anthropic  # type: ignore
            client = anthropic.Anthropic(api_key=self._api_key)
        except ImportError:
            return GenerationResult(
                success=False,
                error="anthropic package not installed. Run: pip install anthropic",
            )
        except Exception as exc:
            return GenerationResult(success=False, error=f"Client init failed: {exc}")

        prompt = self._build_prompt(description, equipment_available, safety_level, max_steps)

        try:
            response = client.messages.create(
                model=self._model,
                max_tokens=4096,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:
            return GenerationResult(success=False, error=f"API call failed: {exc}")

        text = response.content[0].text if response.content else ""
        tokens = response.usage.input_tokens + response.usage.output_tokens

        yaml_str = self._parse_yaml_from_response(text)
        valid, warnings = self._validate(yaml_str)

        if not valid:
            return GenerationResult(
                success=False,
                yaml_string=yaml_str,
                tokens_used=tokens,
                model=self._model,
                warnings=warnings,
                error="Generated YAML failed validation",
            )

        try:
            doc = ProtocolParser().load_string(yaml_str)
        except Exception as exc:
            return GenerationResult(
                success=False,
                yaml_string=yaml_str,
                tokens_used=tokens,
                model=self._model,
                warnings=warnings,
                error=f"Protocol parse failed: {exc}",
            )

        return GenerationResult(
            success=True,
            protocol=doc,
            yaml_string=yaml_str,
            tokens_used=tokens,
            model=self._model,
            warnings=warnings,
        )

    def refine(
        self,
        doc,  # ProtocolDocument
        feedback: str,
    ) -> GenerationResult:
        """Refine an existing protocol based on feedback."""
        if not self._api_key:
            return GenerationResult(
                success=False,
                error="No API key provided.",
            )

        try:
            import anthropic  # type: ignore
            client = anthropic.Anthropic(api_key=self._api_key)
        except Exception as exc:
            return GenerationResult(success=False, error=str(exc))

        import yaml as _yaml
        # Serialise existing protocol as YAML context
        existing_yaml = _yaml.dump({
            "metadata": {
                "name": doc.metadata.name,
                "version": doc.metadata.version,
                "author": doc.metadata.author,
                "safety_level": doc.metadata.safety_level,
            },
            "steps": [
                {"id": s.id, "name": s.name, "type": s.type, "params": s.params}
                for s in doc.steps
            ],
        }, default_flow_style=False)

        prompt = (
            f"Here is an existing OpenLab protocol:\n\n```yaml\n{existing_yaml}\n```\n\n"
            f"Please refine it based on this feedback:\n{feedback}\n\n"
            "Output the complete updated protocol YAML enclosed in ```yaml ... ``` fences."
        )

        try:
            response = client.messages.create(
                model=self._model,
                max_tokens=4096,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:
            return GenerationResult(success=False, error=f"API call failed: {exc}")

        text = response.content[0].text if response.content else ""
        tokens = response.usage.input_tokens + response.usage.output_tokens
        yaml_str = self._parse_yaml_from_response(text)
        valid, warnings = self._validate(yaml_str)

        if not valid:
            return GenerationResult(
                success=False,
                yaml_string=yaml_str,
                tokens_used=tokens,
                model=self._model,
                warnings=warnings,
                error="Refined YAML failed validation",
            )

        try:
            new_doc = ProtocolParser().load_string(yaml_str)
        except Exception as exc:
            return GenerationResult(
                success=False, yaml_string=yaml_str,
                tokens_used=tokens, model=self._model,
                warnings=warnings, error=str(exc),
            )

        return GenerationResult(
            success=True, protocol=new_doc, yaml_string=yaml_str,
            tokens_used=tokens, model=self._model, warnings=warnings,
        )

    # ── Private helpers ───────────────────────────────────────────────────────

    def _build_prompt(
        self,
        description: str,
        equipment: Optional[List[str]],
        safety_level: str,
        max_steps: int = 30,
    ) -> str:
        lines = [f"Generate an OpenLab protocol for the following:\n\n{description}\n"]
        if equipment:
            lines.append(f"Available equipment: {', '.join(equipment)}")
        lines.append(f"Safety level: {safety_level}")
        lines.append(f"Maximum steps: {max_steps}")
        lines.append(
            "\nOutput ONLY the YAML protocol enclosed in ```yaml ... ``` code fences."
        )
        return "\n".join(lines)

    def _parse_yaml_from_response(self, text: str) -> str:
        """Extract the YAML block from a markdown response."""
        # Match ```yaml ... ``` or ``` ... ```
        pattern = r"```(?:yaml)?\s*\n(.*?)```"
        match = re.search(pattern, text, re.DOTALL)
        if match:
            return match.group(1).strip()
        # Fallback: return the whole text stripped
        return text.strip()

    def _validate(self, yaml_str: str) -> Tuple[bool, List[str]]:
        """
        Validate YAML syntax and basic protocol schema.
        Returns (is_valid, list_of_warnings).
        """
        warnings: List[str] = []
        if not yaml_str:
            return False, ["Empty YAML string"]

        try:
            data = yaml.safe_load(yaml_str)
        except yaml.YAMLError as exc:
            return False, [f"YAML syntax error: {exc}"]

        if not isinstance(data, dict):
            return False, ["Top-level must be a mapping"]

        if "metadata" not in data:
            warnings.append("Missing 'metadata' section")
        if "steps" not in data or not data.get("steps"):
            return False, ["Missing or empty 'steps' section"]

        steps = data.get("steps", [])
        for s in steps:
            if not isinstance(s, dict):
                return False, ["Each step must be a mapping"]
            if "type" not in s:
                warnings.append(f"Step {s.get('id', '?')} missing 'type'")

        return True, warnings
