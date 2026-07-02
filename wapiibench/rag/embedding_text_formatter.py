from __future__ import annotations

import yaml

HTTP_METHOD_SYNONYMS = {
    "get": ("Get", "Read", "Fetch", "Retrieve"),
    "put": ("Put", "Update", "Modify"),
    "post": ("Post", "Create", "Add", "Append", "Insert"),
    "delete": ("Delete", "Remove"),
    "options": ("Check", "Inspect"),
    "patch": ("Patch", "Modify", "Update"),
}


def format_embedding_text_for_operation_with_description(path_name: str, method: str, operation: dict) -> str:
    description = operation.get("description", "")

    return f"'{description}', using '{', '.join(HTTP_METHOD_SYNONYMS.get(method, (method,)))}' on '{path_name}'"


def format_embedding_text_for_operation_with_description_and_parameters(path_name: str, method: str,
                                                                        operation: dict) -> str | None:
    description = operation.get("description", "")
    all_parameters = operation.get("parameters", [])
    if len(all_parameters) == 0:
        return None

    return (f"{description}, using {', '.join(HTTP_METHOD_SYNONYMS.get(method, (method,)))} on {path_name}.\n"
            f"Supported parameters: {make_embedding_string_for_parameters(all_parameters)}")


def format_embedding_text_for_operation_with_description_and_request_body(path_name: str, method: str,
                                                                          operation: dict) -> str | None:
    description = operation.get("description", "")
    if not "requestBody" in operation:
        return None

    return (f"{description}, using {', '.join(HTTP_METHOD_SYNONYMS.get(method, (method,)))} on {path_name}.\n"
            f"Supported request body: {make_embedding_string_for_request_body(operation.get('requestBody', {}))}")


def format_embedding_text_for_operation_with_description_and_response_schema(path_name: str, method: str,
                                                                             operation: dict) -> str | None:
    description = operation.get("description", "")
    response = _get_200_or_default_response(operation)
    if not response:
        return None

    response_schema_text = make_embedding_string_for_response_schema(response)
    if not response_schema_text:
        return None

    return (f"{description}, using {', '.join(HTTP_METHOD_SYNONYMS.get(method, (method,)))} on {path_name}.\n"
            f"Supported response schema: {response_schema_text}")


def format_embedding_text_for_operation_with_description_and_response_example(path_name: str, method: str,
                                                                              operation: dict) -> str | None:
    description = operation.get("description", "")
    response = _get_200_or_default_response(operation)
    if not response:
        return None

    response_example_text = make_embedding_string_for_response_example(response)
    if not response_example_text:
        return None

    return (f"{description}, using {', '.join(HTTP_METHOD_SYNONYMS.get(method, (method,)))} on {path_name}.\n"
            f"Supported response example:\n{response_example_text}")


def make_embedding_string_for_parameters(parameters: list[dict]) -> str:
    parameter_strings = []
    for parameter in parameters:
        parameter_strings.append(f"{parameter.get('name')}: {parameter.get('description', '-')}")
    return "  \n".join(parameter_strings)


def make_embedding_string_for_request_body(request_body: dict) -> str:
    """
    Converts requestBody YAML to a compact retrieval string.
    """
    if not request_body:
        return ""

    content = request_body.get("content", {})
    if not content:
        return ""

    media_type, schema = pick_preferred_media_type(content)
    schema = schema.get("schema", {})
    if not schema:
        raise ValueError(f"No schema found for media type {media_type}")

    embedding_strings = []
    for schema in schema["oneOf"] if "oneOf" in schema else [schema]:

        if (properties := schema.get("properties")) is not None:
            if (additional_properties := schema.get("additionalProperties")) is not None \
                    and not isinstance(additional_properties, bool):
                raise NotImplementedError(f"{additional_properties = }")
            try:
                property_strings = _handle_properties_key(properties)
            except ValueError as e:
                raise ValueError(f"Error processing properties for schema {request_body}") from e
            embedding_strings.append(
                f"{schema.get('description', schema.get('type', '-'))}:\n  " + "\n  ".join(property_strings))

        else:
            embedding_strings.append(schema.get('description', schema.get('type', '-')))

    return "\n".join(embedding_strings)


def make_embedding_string_for_response_schema(response: dict) -> str:
    if not response:
        return ""

    content = response.get("content", {})
    if not isinstance(content, dict) or not content:
        return ""

    _, media_obj = pick_preferred_media_type(content)
    if not isinstance(media_obj, dict):
        return ""

    schema = media_obj.get("schema")
    if not isinstance(schema, dict):
        return ""

    if "properties" in schema and isinstance(schema["properties"], dict):
        property_strings = _handle_properties_key(schema["properties"])
        return f"{schema.get('description', schema.get('type', '-'))}:\n  " + "\n  ".join(property_strings)

    # Fallback for non-object or truncated schemas
    return yaml.safe_dump(schema, sort_keys=False, width=10000).strip()


def make_embedding_string_for_response_example(response: dict) -> str:
    if not response:
        return ""

    content = response.get("content", {})
    if not isinstance(content, dict) or not content:
        return ""

    _, media_obj = pick_preferred_media_type(content)
    if not isinstance(media_obj, dict):
        return ""

    examples = media_obj.get("examples")
    if not isinstance(examples, dict) or not examples:
        return ""

    first_example = next(iter(examples.values()))
    if isinstance(first_example, dict) and "value" in first_example:
        first_example = first_example["value"]

    return yaml.safe_dump(first_example, sort_keys=False, width=10000).strip()


def _get_200_or_default_response(operation: dict) -> dict | None:
    responses = operation.get("responses", {})
    if not isinstance(responses, dict):
        return None

    for response_key in ("200", 200, "default"):
        response = responses.get(response_key)
        if isinstance(response, dict):
            return response

    return None


def pick_preferred_media_type(content: dict) -> tuple[str, dict]:
    preferred_order = (
        "application/json",
        "application/x-www-form-urlencoded",
        "multipart/form-data",
        "text/x-markdown",
        "text/plain",
        "*/*",
    )

    for preferred in preferred_order:
        if preferred in content:
            value = content.get(preferred)
            return preferred, value if isinstance(value, dict) else {}

    first_key = next(iter(content.keys()))
    first_value = content.get(first_key)
    return first_key, first_value if isinstance(first_value, dict) else {}


def _handle_properties_key(properties: dict) -> list[str]:
    property_strings = []
    for name, prop in properties.items():
        type_str = prop.get("type", "unknown")
        if type_str == "array":
            array_description = prop.get("description", "")
            items = prop.get("items", {})
            items_description = items.get("description", items.get("type", ""))
            property_strings.append(f"{name}: {array_description} -> Item one of ({items_description}):")

            nested_properties = items.get("properties", {})
            property_strings.extend(map(lambda s: f"  {s}", _handle_properties_key(nested_properties)))
        else:
            property_strings.append(f"{name}: {prop.get('description', '-')}")
            nested_properties = prop.get("properties", {})
            property_strings.extend(map(lambda s: f"  {s}", _handle_properties_key(nested_properties)))
    return property_strings
