"""
Script to match URLs from test_data.json to OpenAPI spec paths.
"""

import json
import re
from pathlib import Path
from typing import Optional

import yaml

# API name to spec file mapping
_API_TO_SPEC = {
    "asana": "asana.yaml",
    "google_calendar_v3": "google_calendar_v3.yaml",
    "google_sheet_v4": "google_sheet_v4.yaml",
    "slack": "slack.yaml",
}

# API name to expected URL prefix (extracted from OpenAPI spec server URLs)
_API_PREFIXES = {
    "asana": "https://app.asana.com/api/1.0",
    "google_calendar_v3": "https://www.googleapis.com/calendar/v3",
    "google_sheet_v4": "https://sheets.googleapis.com",
    "slack": "https://slack.com/api",
}


def _load_spec(spec_path: Path) -> dict:
    """Load an OpenAPI spec from a YAML file."""
    with open(spec_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _get_spec_paths(spec: dict) -> dict:
    """Extract all paths from the OpenAPI spec."""
    return spec.get("paths", {})


def _extract_action_suffix(path: str) -> tuple[str, str]:
    """
    Extract action suffix from path like :copyTo, :append, :clear, :batchUpdate.
    These Google-style action suffixes appear at the very end of the path.

    Returns:
        Tuple of (path_without_suffix, suffix)
    """
    # Known Google API action suffixes
    action_pattern = r":(copyTo|append|clear|batchUpdate|batchUpdateByDataFilter|getByDataFilter)$"
    match = re.search(action_pattern, path)
    if match:
        return path[:match.start()], match.group(0)
    return path, ""


def _normalize_path_parts(path: str, is_url: bool = False) -> tuple[list[str], str]:
    """
    Split a path into parts and normalize path parameters.
    Path parameters like {attachment_gid} are replaced with a placeholder.

    :param path: The path to normalize
    :param is_url: If True also recognizes <param> style placeholders (from test data)

    :return: Tuple of (normalized_parts, action_suffix)
    """
    # First, extract any action suffix
    path_without_suffix, action_suffix = _extract_action_suffix(path)

    parts = [p for p in path_without_suffix.split("/") if p]
    normalized = []
    for part in parts:
        # Check if this part is a path parameter
        # OpenAPI style: {id}, {attachment_gid}
        # Test data style: <spreadsheetId>, <calendarId>
        if re.match(r"^\{[^}]+}$", part) or (is_url and re.match(r"^<[^>]+>$", part)):
            normalized.append("{param}")
        else:
            normalized.append(part)
    return normalized, action_suffix


def _match_url_to_path(url: str, method: str, api: str, spec_paths: dict) -> Optional[str]:
    """
    Match a URL to a path in the OpenAPI spec.

    :param url: The full URL from test_data.json
    :param method: The HTTP method (get, post, etc.)
    :param api: The API name
    :param spec_paths: The paths dictionary from the OpenAPI spec

    :return: The matched path from the spec, or None if no match found
    """
    # Get the expected prefix for this API
    prefix = _API_PREFIXES.get(api)
    if not prefix:
        raise ValueError(f"Unknown API: {api}")

    # Verify and remove the prefix
    if not url.startswith(prefix):
        raise ValueError(
            f"URL '{url}' does not start with expected prefix '{prefix}' for API '{api}'"
        )

    # Get the path part of the URL (without prefix)
    url_path = url[len(prefix):]
    if not url_path:
        url_path = "/"

    # Normalize the URL path parts (is_url=True to recognize <param> style)
    url_parts, url_action = _normalize_path_parts(url_path, is_url=True)

    # Try to match against spec paths
    http_method_lower = method.lower()

    for spec_path, path_item in spec_paths.items():
        if http_method_lower not in path_item:
            continue

        spec_parts, spec_action = _normalize_path_parts(spec_path, is_url=False)

        if url_action != spec_action:
            continue

        if len(url_parts) != len(spec_parts):
            continue

        match = True
        for url_part, spec_part in zip(url_parts, spec_parts):
            if spec_part == "{param}":
                continue
            elif url_part != spec_part:
                match = False
                break

        if match:
            return spec_path

    return None


def match_urls():
    base_dir = Path(__file__).parent.parent.parent
    data_path = base_dir / "data" / "synthetic" / "all" / "test_data_final.json"
    specs_dir = base_dir / "openapi" / "real_world_specs"

    # Load test data
    with open(data_path, "r", encoding="utf-8") as f:
        test_data = json.load(f)

    # Load all specs
    specs = {}
    for api, spec_file in _API_TO_SPEC.items():
        spec_path = specs_dir / spec_file
        specs[api] = _load_spec(spec_path)

    errors = []

    for i, entry in enumerate(test_data):
        api = entry["api"]
        url = entry["config"]["url"]
        method = entry["config"]["method"]

        spec_paths = _get_spec_paths(specs[api])

        try:
            matched_path = _match_url_to_path(url, method, api, spec_paths)

            if matched_path:
                # Insert url_clean right after url in config
                new_config = {}
                for key, value in entry["config"].items():
                    new_config[key] = value
                    if key == "url":
                        new_config["url_clean"] = matched_path
                entry["config"] = new_config
            else:
                errors.append({
                    "index": i,
                    "api": api,
                    "url": url,
                    "method": method,
                    "error": "No matching path found",
                })
        except ValueError as e:
            errors.append({
                "index": i,
                "api": api,
                "url": url,
                "method": method,
                "error": str(e),
            })

    print(f"Total entries: {len(test_data)}")
    print(f"Successfully matched: {len(test_data) - len(errors)}")
    print(f"Errors/No match: {len(errors)}")
    print()

    if errors:
        print("=== Errors/No matches ===")
        for error in errors:
            print(f"  [{error['index']}] {error['method'].upper()} {error['url']}")
            print(f"       Error: {error['error']}")
    else:
        # No errors - write cleaned output file
        output_path = base_dir / "data" / "test" / "test_data_cleaned.json"
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(test_data, f, indent=2)
        print(f"Output written to: {output_path}")

    return errors


if __name__ == "__main__":
    match_urls()
