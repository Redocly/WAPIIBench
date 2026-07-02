from __future__ import annotations

MAX_REF_RESOLUTION_LEVEL = 2
HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
SECURITY_SCHEMES = {"apiKey": "API key", "http": "HTTP", "oauth2": "OAuth2", "openIdConnect": "OpenID Connect"}


def preprocess_yaml_openapi_spec(root: dict) -> dict:
    """
    Resolve all $ref instances and security requirements, then merge all "allOf" schemas.
    :param root: The YAML object to process
    :return: The fully resolved and merged object
    """
    root["paths"] = _preprocess_recursively(root["paths"], root)
    root.pop("security", None)
    merged = _merge_allof(root)
    assert isinstance(merged, dict)
    return merged


def _preprocess_recursively(obj: object, root: dict, seen: set | None = None, ref_level: int = 0,
                            nesting_level: int = 0) -> object:
    """
    Recursively resolve all $ref instances and security requirements in a YAML object.
    :param obj: The object to resolve refs in (dict, list, or primitive)
    :param root: The root document (used for resolving references)
    :param seen: Set of reference paths already visited (for cycle detection)
    :param ref_level: The current $ref inlining level
    :param nesting_level: The current nesting level
    :return: The resolved object
    """
    if seen is None:
        seen = set()

    if isinstance(obj, dict):

        if (ref_path := obj.pop("$ref", None)) is not None:
            assert isinstance(ref_path, str)

            # $ref inlining is only done up to some depth
            if ref_level < MAX_REF_RESOLUTION_LEVEL:
                if not ref_path.startswith("#"):
                    raise ValueError(f"External reference {ref_path} not supported")

                if ref_path in seen:
                    raise ValueError(f"Cycle detected for reference {ref_path}")

                seen = seen.copy()
                seen.add(ref_path)

                resolved = _resolve_json_pointer(root, ref_path)
                if resolved is None:
                    raise ValueError(f"Could not resolve reference {ref_path}")

                if resolved.get("type") == "array":
                    resolved["type"] = ref_path.split("/")[-1]

                processed = _preprocess_recursively(resolved, root, seen, ref_level + 1, nesting_level + 1)
                assert isinstance(processed, dict)
                return processed | obj  # fields of the reference object override those of the referenced component

            else:
                obj["type"] = ref_path.split("/")[-1]
                return obj

        processed = {key: _preprocess_recursively(value, root, seen, ref_level, nesting_level + 1)
                     for key, value in obj.items()}

        # If this is a path item object, add all path-level parameters to the operation's parameter list.
        if nesting_level == 1:
            assert any(method in obj for method in HTTP_METHODS)
            _add_path_level_parameters(processed)

        # If this is an operation object, add all securities to the operation's parameter list.
        elif nesting_level == 2:
            assert "security" in obj or "responses" in obj or "operationId" in obj or "requestBody" in obj
            _resolve_securities(processed, root)

        return processed

    elif isinstance(obj, list):
        return [_preprocess_recursively(item, root, seen, ref_level, nesting_level + 1) for item in obj]

    else:
        return obj


def _resolve_json_pointer(root: dict, ref_path: str) -> dict | None:
    """
    Resolve a JSON pointer reference like "#/components/schemas/User".
    :param root: The root document
    :param ref_path: The reference path (e.g., "#/components/schemas/User")
    :return: The resolved object, or None if not found
    """
    if not ref_path.startswith("#"):
        return None

    # Remove the leading "#" and split by "/"
    path = ref_path[1:]
    if path.startswith("/"):
        path = path[1:]

    if not path:
        return root

    parts = path.split("/")

    current = root
    for part in parts:
        part = part.replace("~1", "/").replace("~0", "~")

        if isinstance(current, dict):
            if part in current:
                current = current[part]
            else:
                return None
        elif isinstance(current, list):
            try:
                index = int(part)
                current = current[index]
            except (ValueError, IndexError):
                return None
        else:
            return None

    return current


def _resolve_securities(obj: dict, root: dict) -> None:
    """
    Resolve global and operation-level security requirements and add them to the operation's parameter list.
    :param obj: The operation object to modify in place
    :param root: The root document
    """
    global_securities = {name for security in root.get("security", []) for name in security.keys()}
    operation_securities = {name for security in obj.pop("security", []) for name in security.keys()}
    securities = sorted(global_securities | operation_securities)  # remove duplicates and ensure deterministic order
    if securities:
        security_params = {}
        for scheme_name in securities:
            security_schema = root["components"]["securitySchemes"][scheme_name]
            type = security_schema["type"]
            name = security_schema["name"] if type == "apiKey" else "Authorization"
            location = security_schema["in"] if type == "apiKey" else "header"
            description = f"HTTP ({security_schema['scheme']}) security scheme" if type == "http" else f"{SECURITY_SCHEMES[type]} security scheme"
            if (security_param := security_params.get((name, location))) is not None:
                if description not in security_param["description"]:
                    security_param["description"] += " | " + description
            else:
                security_param = {
                    "name": name,
                    "in": location,
                    "description": description,
                    "required": True,
                    "schema": {"type": "string"},
                }
                if security_schema.get("deprecated", False):
                    security_param["deprecated"] = True
                security_params[(name, location)] = security_param
        # Security parameters are required by default unless there are alternative ones with different names and locations.
        if len(security_params) > 1:
            for security_param in security_params.values():
                security_param["required"] = False
        if "parameters" in obj:
            obj["parameters"].extend(security_params.values())
        else:
            obj["parameters"] = list(security_params.values())


def _add_path_level_parameters(obj: dict) -> None:
    """
    Add the path-level parameters to the operation's parameter list. Also, if necessary, add the path's summary and description to the operation.
    :param obj: The path item object to modify in place
    """
    path_level_summary = obj.pop("summary", "")
    path_level_description = obj.pop("description", "")
    path_level_params = obj.pop("parameters", [])

    for key, operation_obj in obj.items():
        if key not in HTTP_METHODS:
            continue

        if path_level_summary:
            operation_obj.setdefault("summary", path_level_summary)
        if path_level_description:
            operation_obj.setdefault("description", path_level_description)

        if path_level_params:
            if operation_level_params := operation_obj.get("parameters"):
                existing_params = {(param["name"], param["in"]) for param in operation_level_params}
                for path_level_param in path_level_params:
                    param_key = (path_level_param["name"], path_level_param["in"])
                    if param_key not in existing_params:
                        operation_level_params.append(path_level_param)
                        existing_params.add(param_key)
            else:
                operation_obj["parameters"] = path_level_params


def _merge_allof(obj: object) -> object:
    """
    Recursively merge all "allOf" schemas into single objects.
    :param obj: The object to process (dict, list, or primitive)
    :return: The object with all "allOf" schemas merged
    """
    if isinstance(obj, dict):
        # First, recursively process all values
        processed = {key: _merge_allof(value) for key, value in obj.items()}

        # Check if this dict has an allOf key
        if (allof_list := processed.get("allOf")) is not None:
            if isinstance(allof_list, list):
                # Merge all schemas in allOf
                merged = {}
                for schema in allof_list:
                    if isinstance(schema, dict):
                        _deep_merge(merged, schema)

                # Remove allOf and merge remaining keys with the merged result
                del processed["allOf"]
                _deep_merge(merged, processed)
                return merged

        return processed

    elif isinstance(obj, list):
        return [_merge_allof(item) for item in obj]

    else:
        return obj


def _deep_merge(target: dict, source: dict) -> None:
    """
    Deep merge source dict into target dict. Arrays are concatenated, dicts are recursively merged, other values are overwritten.
    :param target: The target dict to merge into (modified in place)
    :param source: The source dict to merge from
    """
    for key, value in source.items():
        if key in target:
            if isinstance(target[key], dict) and isinstance(value, dict):
                _deep_merge(target[key], value)
            elif isinstance(target[key], list) and isinstance(value, list):
                # Concatenate lists
                target[key] += value
            else:
                # Overwrite
                target[key] = value
        else:
            target[key] = value
