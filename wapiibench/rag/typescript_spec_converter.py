from __future__ import annotations

import json
from textwrap import indent

import regex as re

from rag.embedding_text_formatter import pick_preferred_media_type

PRIMITIVE_TYPE_MAP = {
    "string": "string",
    "number": "number",
    "integer": "number",
    "boolean": "boolean",
    "null": "null",
}


def convert_operation_to_typescript(path_name: str, method: str, operation: dict) -> str:
    """Convert an OpenAPI operation object into a TypeScript type declaration."""
    spec_fields: list[str] = []

    path_params, query_params, header_params = _extract_parameter_entries(operation.get("parameters", []))
    if path_params:
        spec_fields.append(f"path: {_render_object_type(path_params)}")
    if query_params:
        spec_fields.append(f"query: {_render_object_type(query_params)}")
    if header_params:
        spec_fields.append(f"header: {_render_object_type(header_params)}")

    request_body = operation.get("requestBody")
    request_body_field = _render_request_body_field(request_body)
    if request_body_field is not None:
        spec_fields.append(request_body_field)

    if spec_fields:
        spec_body = "{\n" + indent("\n".join(spec_fields), "  ") + "\n}"
    else:
        spec_body = "{}"

    type_name = _build_request_type_name(operation.get("operationId"), method, path_name)
    operation_summary = _inline_comment_text(operation.get("summary") or operation.get("description") or "")
    return f"// {method.upper()} {path_name}\n// {operation_summary}\ntype {type_name} = {spec_body}"


def _extract_parameter_entries(parameters: list[dict]) -> tuple[
    list[tuple[str, str, bool, str]],
    list[tuple[str, str, bool, str]],
    list[tuple[str, str, bool, str]]
]:
    """Extract separate path, query, and header parameter lists."""
    if not isinstance(parameters, list):
        return [], [], []

    params_by_location: dict[str, list[tuple[str, str, bool, str]]] = {"path": [], "query": [], "header": []}

    for parameter in parameters:
        if not isinstance(parameter, dict):
            continue

        name = parameter.get("name")
        if not isinstance(name, str) or not name:
            continue

        schema = parameter.get("schema")
        if not isinstance(schema, dict):
            schema = _extract_schema_from_content(parameter.get("content"))
        if schema is None:
            schema = {"type": "string"}

        rendered_type = _render_schema(schema)
        required = parameter.get("required", False)
        description = _inline_comment_text(parameter.get("description", ""))
        location = parameter.get("in", "").lower()

        params_by_location[location].append((name, rendered_type, required, description))

    return params_by_location["path"], params_by_location["query"], params_by_location["header"]


def _render_request_body_field(request_body: dict | None) -> str | None:
    """Render the request body parameter list."""
    if not isinstance(request_body, dict):
        return None

    content = request_body.get("content")
    if not isinstance(content, dict) or not content:
        return None

    _, media_type_schema = pick_preferred_media_type(content)
    schema = None
    if isinstance(media_type_schema, dict):
        candidate = media_type_schema.get("schema")
        if isinstance(candidate, dict):
            schema = candidate
    if schema is None:
        schema = {}

    required = bool(request_body.get("required", True))
    optional_suffix = "" if required else "?"
    rendered_schema = _render_schema(schema)
    return f"requestBody{optional_suffix}: {rendered_schema}"


def _extract_schema_from_content(content: dict | None) -> dict | None:
    """Extract the schema with the preferred media type."""
    if not isinstance(content, dict) or not content:
        return None

    _, media_type_schema = pick_preferred_media_type(content)
    schema = media_type_schema.get("schema")
    return schema if isinstance(schema, dict) else None


def _render_schema(schema: dict | None, render_description: bool = True) -> str:
    """Recursively render an arbitrary schema."""
    if not isinstance(schema, dict):
        return "{ /* ... */ }"

    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and enum_values:
        return " | ".join(_literal_to_ts(value) for value in enum_values)

    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        union_items = []
        for item_type in schema_type:
            if isinstance(item_type, str):
                union_items.append(PRIMITIVE_TYPE_MAP.get(item_type, "{ /* ... */ }"))
        if union_items:
            # Preserve order and deduplicate
            deduped = list(dict.fromkeys(union_items))
            return " | ".join(deduped)
        return "{ /* ... */ }"

    if isinstance(schema_type, str):
        if schema_type in PRIMITIVE_TYPE_MAP:
            return PRIMITIVE_TYPE_MAP[schema_type]

        if schema_type == "array":
            items = schema.get("items")
            item_type = _render_schema(items if isinstance(items, dict) else {}, render_description=False)
            return f"{item_type}[]"

    properties = schema.get("properties")
    if isinstance(properties, dict):
        entries: list[tuple[str, str, bool, str]] = []
        for prop_name, prop_schema in properties.items():
            if not isinstance(prop_name, str):
                continue
            prop_type = _render_schema(prop_schema if isinstance(prop_schema, dict) else {}, render_description=False)
            description = _inline_comment_text(prop_schema.get("description", ""), target_length=50) \
                if render_description else ""
            entries.append((prop_name, prop_type, bool(prop_schema.get("required", True)), description))
        return _render_object_type(entries)

    return schema_type if schema_type else "{ /* ... */ }"


def _render_object_type(entries: list[tuple[str, str, bool, str]]) -> str:
    """Render the properties of an object."""
    if not entries:
        return "{}"

    rendered_entries = []
    for name, rendered_type, required, description in entries:
        optional_suffix = "" if required else "?"
        if rendered_type.startswith("{\n"):
            line = f"{_render_property_name(name)}{optional_suffix}: {rendered_type}"
        else:
            description = f" // {description}" if description else ""
            line = f"{_render_property_name(name)}{optional_suffix}: {rendered_type}{description}"
        rendered_entries.append(line)

    return "{\n" + indent("\n".join(rendered_entries), "  ") + "\n}"


def _render_property_name(name: str) -> str:
    """Add quotes around non-standard property names."""
    if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", name):
        return name
    return json.dumps(name)


def _literal_to_ts(value) -> str:
    """Convert a value to a TypeScript-compatible string representation."""
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    return str(value)


def _inline_comment_text(text: str, target_length: int = 100) -> str:
    """Shorten and format a description text."""
    text = text.lstrip().split("\n", 1)[0]  # take only the first line
    if len(text) > target_length:
        end_idx = text.rfind(".", 0, target_length + 50)  # cut after a sentence
        if end_idx == -1:
            end_idx = text.rfind(" ", 0, target_length + 50)  # cut after a word
        text = text[:end_idx + 1].rstrip() if end_idx != -1 else text[:target_length]
    return text


def _build_request_type_name(operation_id: str | None, method: str, path: str) -> str:
    """Build a TypeScript type name for a request object."""
    if not operation_id:
        # Build a dummy operationId by appending method and path (without path parameters)
        operation_id = method + re.sub(r"\{[^}]*}", "", path)

    # Convert the operationId to PascalCase and append "Request"
    return re.sub(r"(?:^|[^a-zA-Z0-9]+)([a-zA-Z0-9])", lambda match: match.group(1).upper(), operation_id) + "Request"
