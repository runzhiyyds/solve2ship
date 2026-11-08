"""Versioned structural task compiler; no semantic interpretation or execution."""

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Protocol


MAX_TASK_BYTES = 262144
MAX_SAMPLES = 500
COMPILER_VERSION = "structural-v3"


class ContractError(ValueError):
    def __init__(self, code, field="definition"):
        self.code, self.field = code, field
        super().__init__(code)


def strict_json_loads(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ContractError("invalid_json")
            result[key] = value
        return result

    def invalid_constant(_):
        raise ContractError("invalid_json")

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ContractError("invalid_json") from error


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def validate_json(value, *, max_string_length=50000):
    """Bound nesting/nodes and reject unsafe JSON numbers, surrogates and non-JSON types."""
    pending = [(value, 0)]
    count = 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if count > 20000 or depth > 20:
            raise ContractError("invalid_json_value")
        if item is None or type(item) is bool:
            continue
        if type(item) is str:
            if len(item) > max_string_length or any(0xD800 <= ord(char) <= 0xDFFF for char in item):
                raise ContractError("invalid_json_value")
        elif type(item) is int:
            if abs(item) > 9007199254740991:
                raise ContractError("invalid_json_value")
        elif type(item) is float:
            if not math.isfinite(item) or (item.is_integer() and abs(item) > 9007199254740991):
                raise ContractError("invalid_json_value")
        elif type(item) is list:
            pending.extend((child, depth + 1) for child in item)
        elif type(item) is dict:
            if any(type(key) is not str for key in item):
                raise ContractError("invalid_json_value")
            pending.extend((child, depth + 1) for child in item.values())
            pending.extend((key, depth + 1) for key in item)
        else:
            raise ContractError("invalid_json_value")


def _strings(value, field):
    if type(value) is not list or len(value) > 32:
        raise ContractError("invalid_field", field)
    if any(type(item) is not str or not item.strip() or len(item) > 2000 for item in value):
        raise ContractError("invalid_field", field)
    return value


def parse_source(value):
    """Allow raw upload text to be parsed on the server without browser JSON data loss."""
    if type(value) is not dict:
        return value
    if value.keys() != {"format", "content"} or value['format'] not in ('json', 'jsonl') or type(value['content']) is not str:
        raise ContractError("invalid_json")
    text = value['content']
    if value['format'] == 'json':
        return strict_json_loads(text)
    lines = [line for line in text.split('\n') if line.strip()]
    if len(lines) > MAX_SAMPLES:
        raise ContractError("invalid_samples", "samples")
    return [strict_json_loads(line) for line in lines]


def compile_task(definition):
    """Pure and deterministic. Preserve intent; unresolved semantics stay unresolved."""
    if type(definition) is not dict:
        raise ContractError("invalid_field")
    validate_json(definition, max_string_length=MAX_TASK_BYTES)
    if len(canonical_json(definition).encode("utf-8")) > MAX_TASK_BYTES:
        raise ContractError("task_too_large")
    fields = {"title", "samples", "output_specification", "labels", "constraints",
              "technical_preferences", "resources", "budget"}
    if definition.keys() - fields:
        raise ContractError("invalid_field", "unknown_fields")
    samples = parse_source(definition.get("samples"))
    if type(samples) is not list or not 1 <= len(samples) <= MAX_SAMPLES:
        raise ContractError("invalid_samples", "samples")
    validate_json(samples)
    specification = definition.get("output_specification")
    if type(specification) is not str or not specification.strip() or len(specification) > 20000:
        raise ContractError("invalid_specification", "output_specification")
    title = definition.get("title", "Untitled task")
    if type(title) is not str or not title.strip() or len(title) > 120:
        raise ContractError("invalid_field", "title")
    labels = parse_source(definition.get("labels", []))
    if type(labels) is not list:
        raise ContractError("invalid_labels", "labels")
    validate_json(labels)
    labelled = set()
    for label in labels:
        if type(label) is not dict or label.keys() != {"sample_index", "expected_output"}:
            raise ContractError("invalid_labels", "labels")
        index = label["sample_index"]
        if type(index) is not int or not 0 <= index < len(samples) or index in labelled:
            raise ContractError("invalid_labels", "labels")
        labelled.add(index)
    constraints = definition.get("constraints", {"hard": [], "soft": []})
    if type(constraints) is not dict or constraints.keys() - {"hard", "soft"}:
        raise ContractError("invalid_field", "constraints")
    constraints = {kind: _strings(constraints.get(kind, []), f"constraints.{kind}") for kind in ("hard", "soft")}
    preferences = _strings(definition.get("technical_preferences", []), "technical_preferences")
    resources = _strings(definition.get("resources", []), "resources")
    budget = definition.get("budget", {})
    if type(budget) is not dict or budget.keys() - {"mode", "max_cost_usd", "max_cost_cny", "max_candidate_executions"}:
        raise ContractError("invalid_field", "budget")
    cost, executions = budget.get("max_cost_usd", 0), budget.get("max_candidate_executions", 0)
    cny = budget.get('max_cost_cny', 0)
    if type(cny) not in (int, float) or not 0 <= cny <= 1000000:
        raise ContractError('invalid_field', 'budget.max_cost_cny')
    if type(cost) not in (int, float) or not 0 <= cost <= 1000000:
        raise ContractError("invalid_field", "budget.max_cost_usd")
    if type(executions) is not int or not 0 <= executions <= 100000:
        raise ContractError("invalid_field", "budget.max_candidate_executions")
    budget_mode = budget.get('mode', 'explicit' if 'budget' in definition else 'inherit_work')
    if budget_mode not in ('explicit', 'inherit_work') or (budget_mode == 'inherit_work' and (cost != 0 or cny != 0 or executions != 0)):
        raise ContractError('invalid_field', 'budget.mode')
    normalized = {"title": title, "samples": samples, "output_specification": specification,
                  "labels": sorted(labels, key=lambda row: row["sample_index"]),
                  "constraints": constraints, "technical_preferences": preferences, "resources": resources,
                  "budget": {"mode": budget_mode, "max_cost_usd": cost, "max_cost_cny": cny, "max_candidate_executions": executions}}
    mode = "specification_driven" if not labels else ("supervised" if len(labels) == len(samples) else "hybrid")

    def json_type(value):
        return {type(None): "null", bool: "boolean", int: "integer", float: "number",
                str: "string", list: "array", dict: "object"}[type(value)]

    unknowns = ["output_schema_unconfirmed", "success_criteria_not_executable", "no_dataset_split"]
    if labels:
        unknowns.append("labels_unverified")
    else:
        unknowns.append("no_ground_truth")
    if constraints["hard"] or constraints["soft"]:
        unknowns.append("constraints_not_enforced")
    if resources:
        unknowns.append("resources_unverified")
    contract = {
        "schema_version": 2, "compiler_version": COMPILER_VERSION,
        "semantic_interpretation": False, "definition": normalized,
        "dataset": {"sha256": sha256(samples), "sample_count": len(samples),
                    "input_schema": {"status": "observed_only", "root_types": sorted({json_type(s) for s in samples})},
                    "split": "unassigned"},
        "output_schema": {"status": "unconfirmed", "schema": None},
        "evaluation": {"supervision_mode": mode, "label_count": len(labels),
                       "label_source": "user_provided_unverified" if labels else "none",
                       "status": "not_configured", "quality_claims_allowed": False},
        "budget_enforcement": "not_started", "unresolved": unknowns,
    }
    # Detached JSON values: callers cannot mutate a preview through their input reference.
    contract = json.loads(canonical_json(contract))
    return {"contract": contract, "content_sha256": sha256(contract)}


@dataclass(frozen=True)
class TaskInput:
    """Legacy M0 input boundary; M1 HTTP intake accepts the richer JSON definition."""

    samples: tuple[object, ...]
    output_specification: str
    # Sample indices paired with their optional ground truth; None can be a label.
    labels: tuple[tuple[int, object], ...] = ()


@dataclass(frozen=True)
class CandidateRef:
    """Opaque family/adapter names permit code, ML, prompts and future paradigms."""

    candidate_id: str
    revision_id: str
    family: str
    artifact_sha256: str
    runtime_adapter: str


class SandboxUnavailable(RuntimeError):
    pass


class SandboxProvider(Protocol):
    def execute(self, candidate: CandidateRef) -> object: ...


class DisabledSandbox:
    """Fail closed. Implementations must enforce isolation before enabling execution."""

    available = False

    def execute(self, candidate: CandidateRef) -> object:
        raise SandboxUnavailable("Isolated execution is not configured; execution denied")
