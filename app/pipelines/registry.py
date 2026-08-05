"""Pipeline registry and parameter validation."""

from __future__ import annotations

import copy

from jsonschema import Draft202012Validator
from jsonschema import ValidationError as _JsonSchemaError

from .base import Pipeline


class PipelineNotFound(KeyError):
    pass


class ParamsInvalid(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


class PipelineRegistry:
    def __init__(self):
        self._pipelines: dict[str, Pipeline] = {}

    def register(self, pipeline: Pipeline) -> Pipeline:
        if not pipeline.id:
            raise ValueError(f"{type(pipeline).__name__} must declare an id")
        if pipeline.id in self._pipelines:
            raise ValueError(f"duplicate pipeline id: {pipeline.id}")
        # Fail at import time rather than at request time on a malformed schema.
        Draft202012Validator.check_schema(pipeline.params_schema)
        self._pipelines[pipeline.id] = pipeline
        return pipeline

    def get(self, pipeline_id: str) -> Pipeline:
        try:
            return self._pipelines[pipeline_id]
        except KeyError:
            raise PipelineNotFound(pipeline_id) from None

    def __contains__(self, pipeline_id: object) -> bool:
        return pipeline_id in self._pipelines

    def all(self) -> list[Pipeline]:
        return list(self._pipelines.values())

    def describe_all(self) -> list[dict]:
        return [p.describe() for p in self._pipelines.values()]

    # -- validation --------------------------------------------------------

    def validate_params(self, pipeline_id: str, params: dict | None) -> dict:
        """Validate against the pipeline's schema and fill in defaults.

        Returns the completed parameter set, so stage code can read values
        directly without defensive `.get(..., fallback)` at every use site.
        """
        pipeline = self.get(pipeline_id)
        params = copy.deepcopy(params or {})
        _apply_defaults(pipeline.params_schema, params)

        validator = Draft202012Validator(pipeline.params_schema)
        errors = sorted(validator.iter_errors(params), key=lambda e: list(e.path))
        if errors:
            raise ParamsInvalid([_format_error(e) for e in errors])
        return params


def _format_error(error: _JsonSchemaError) -> str:
    location = ".".join(str(p) for p in error.path) or "(root)"
    return f"{location}: {error.message}"


def _apply_defaults(schema: dict, target: dict) -> None:
    """Recursively fill object defaults declared in a JSON Schema.

    jsonschema validates but does not populate, and we want a fully-formed
    params dict reaching the pipeline.
    """
    if schema.get("type") != "object":
        return
    for key, subschema in (schema.get("properties") or {}).items():
        if not isinstance(subschema, dict):
            continue
        if subschema.get("type") == "object":
            child = target.setdefault(key, {})
            if isinstance(child, dict):
                _apply_defaults(subschema, child)
        elif key not in target and "default" in subschema:
            target[key] = copy.deepcopy(subschema["default"])


registry = PipelineRegistry()
