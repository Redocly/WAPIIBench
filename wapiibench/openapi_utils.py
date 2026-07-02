from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from functools import cache
from typing import NamedTuple

import regex as re
from openapi_parser import parse
from openapi_parser.enumeration import BaseLocation, DataType, OperationMethod, ParameterLocation, SecurityType
from openapi_parser.specification import AnyOf, Array, Boolean, Integer, Number, Object, OneOf, Operation, Parameter, \
    Path, Property, RequestBody, Schema, Security, Server, Specification, String

from generation_rules import GenerationRule, GenerationRuleset

logging.basicConfig(format="%(levelname)s:%(name)s:%(funcName)s:%(message)s")
logger = logging.getLogger(__name__)
logger.setLevel(logging.ERROR)


class Request(NamedTuple):
    path: str
    method: OperationMethod
    parameters: list[Parameter]
    request_body: RequestBody | None


# Define some regex building blocks
INTEGER_VAL = r"-?\d+"
NUMBER_VAL = r"-?\d+(?:\.\d+)?"
STRING_VAL = r"""(?:"(?:[^"\\]|(?:\\.))*"|'(?:[^'\\]|(?:\\.))*'|`(?:[^`\\]|(?:\\.))*`)"""  # string interpolation is not handled explicitly
STRING_VAL_IN_URL = r"[a-zA-Z0-9_.~<>:-]+"  # <, >, and : are not allowed in typical URLs but needed in our dataset
BOOLEAN_VAL = r"(?:true|false)"
VAR_NAME = r"[a-zA-Z_$][a-zA-Z0-9_$.()]*"  # includes dereferencing and method invocation
TEMPLATE_STRING_VAR = fr"\$\{{{VAR_NAME}\}}"
QUOTATION_MARK = r"""(?:"|'|`)"""  # for when it's acceptable to rely on the model to choose matching quotes
DEFINITIONS = fr"(?(DEFINE)(?P<int>{INTEGER_VAL})(?P<num>{NUMBER_VAL})(?P<str>{STRING_VAL})(?P<url_str>{STRING_VAL_IN_URL})(?P<bool>{BOOLEAN_VAL})(?P<var>{VAR_NAME})(?P<str_var>{TEMPLATE_STRING_VAR})(?P<quote>{QUOTATION_MARK}))"

MAX_OBJECTS_IN_ARRAY = 3
MAX_RECURSION_DEPTH = 6


@cache
def parse_spec(file_path: str, strict_enum: bool = False) -> Specification:
    """
    Wrapper for ``openapi_parser.parse`` which implements caching.
    :param file_path: Path or URL to OpenAPI file
    :param strict_enum: Validate content types and string formats against the enums defined in openapi-parser.
        Note that the OpenAPI specification allows for custom values in these properties.
    :return: The API specification object
    """
    return parse(uri=file_path, strict_enum=strict_enum)


@cache
def spec_to_ruleset(file_path: str) -> GenerationRuleset:
    """
    Convert an OpenAPI specification to a generation ruleset that describes valid requests to that API.
    This method is tailored specifically to the JavaScript Axios library.
    :param file_path: Path to the specification file
    :return: A list of generation rules describing valid requests (make sure to reset them in case they were cached and used before)
    """
    logger.info(f"Converting OpenAPI spec to generation ruleset: {file_path=}")

    spec = parse_spec(file_path)
    server_regex = _create_server_regex(spec)
    possible_requests = _get_possible_requests(spec)

    ruleset = _create_method_as_function_ruleset(possible_requests, server_regex)

    logger.info("Ruleset created successfully")
    return ruleset


def _create_server_regex(spec: Specification) -> str:
    """
    Create a regex that matches the server URL(s) of the API, including potential server variables.
    :param spec: The API specification object
    :return: A regex matching the server URLs
    """
    server_urls = [re.escape(server.url) for server in spec.servers]
    server_regex = join_alternatives(server_urls, inner_parentheses=False)
    server_regex = re.sub(  # server variables are handled similarly to path parameters
        r"\\{[^}]+\\}", _create_val_or_var_regex(String(DataType.STRING), True, is_trailing=False), server_regex)
    return server_regex


def _get_possible_requests(spec: Specification) -> dict[str, list[Request]]:
    """
    Extract all relevant information from the specification and pack it into a suitable data structure.
    :param spec: The API specification object
    :return: A dictionary of possible requests for each method
    """
    global_securities = {name for security in spec.security for name in security.keys()}

    # Axios supports explicitly setting a Content-Type header to cause automatic serialization of request body to a specific media type
    content_type_header = Parameter(
        "Content-Type", ParameterLocation.HEADER, schema=String(DataType.STRING), required=False)

    # Extract all possible requests and group them by HTTP method
    possible_requests = defaultdict(list)
    for path in spec.paths:
        for operation in path.operations:

            method = operation.method
            logger.debug(f"Adding request: [{method.name}] {path.url} ({operation.operation_id})")

            parameters = path.parameters + operation.parameters
            if method in (OperationMethod.PUT, OperationMethod.POST, OperationMethod.PATCH):
                logger.debug("Adding Content-Type header")
                parameters.append(content_type_header)

            operation_securities = {name for security in operation.security for name in security.keys()}
            securities = global_securities | operation_securities
            if securities:
                logger.debug("Operation requires authentication")

                security_params = set()  # automatic deduplication
                for scheme_name in securities:
                    security_schema = spec.security_schemas[scheme_name]

                    if security_schema.type is SecurityType.OPEN_ID_CONNECT:
                        logger.warning("Security scheme OpenID Connect is not supported")
                        continue

                    location = ParameterLocation[security_schema.location.name] \
                        if security_schema.location is not None else ParameterLocation.HEADER
                    name = security_schema.name if security_schema.name else "Authorization"

                    logger.debug(
                        f"{scheme_name}: Type = {security_schema.type.name}, Scheme = {security_schema.scheme.name if security_schema.scheme else None}, Location = {location.name}, Name: {name}")

                    security_params.add((name, location))

                # Add the security schemes as regular parameters to this operation. At least one of them must be used.
                # However, implementing this as a constraint is too complicated. Instead, we make security parameters
                # required if there is just one, otherwise they become all optional.
                parameters.extend([
                    Parameter(name, location, schema=String(DataType.STRING), required=len(security_params) == 1) \
                    for name, location in security_params])

            request = Request(path.url, method, parameters, operation.request_body)
            possible_requests[method.value].append(request)

    return possible_requests


def _create_method_as_function_ruleset(possible_requests: dict[str, list[Request]],
                                       server_regex: str) -> GenerationRuleset:
    """
    Create a ruleset that follows the call syntax ``axios.<method>(url[, config])``.
    :param possible_requests: List of possible requests for the API
    :param server_regex: Regex of the URL's server part
    :return: The ruleset
    """
    generation_ruleset = GenerationRuleset()
    # The funnel rule guides the generation to trigger any of the actual rules
    generation_ruleset.append(GenerationRule(
        r"axios\.", r"\(", join_alternatives(list(possible_requests.keys()), inner_parentheses=False) + r"\(",
        name="funnel"))

    # Convert the possible requests into regexes and the regexes into generation rules
    for method, requests in possible_requests.items():
        logger.debug(f"Converting {len(requests)} requests for method [{method}]")

        request_regexes = []

        for request in requests:
            logger.debug(f"Converting request: {request.path} [{request.method.name}]")

            path_regex = _create_path_regex(request)
            arguments_regex = _create_arguments_regex(request)

            # The closing quotation mark is omitted if the URL ends with a concatenation, as indicated by the "trailing_concat" flag.
            # To avoid an "unknown group" error, we only use this conditional logic if the flag exists in the regex.
            closing_quote = r"(?(trailing_concat)|(?&quote))" if r"(?P<trailing_concat>)" in path_regex else r"(?&quote)"
            request_regexes.append(f"{path_regex}{closing_quote}{arguments_regex}")

        request_regex = join_alternatives(request_regexes)

        starter = fr"axios\.{method}"
        stopper = r"\)\s*(?:;|\n|\.then\(|\.catch\(|\.finally\()"  # asserting that parentheses are balanced is too complicated
        body = fr"{DEFINITIONS}\(\s*(?&quote){server_regex}{request_regex}\s*{stopper}"
        generation_ruleset.append(GenerationRule(starter, stopper, body, name=method))

    return generation_ruleset


def _create_path_regex(request: Request) -> str:
    """
    Create a regex that describes the path in the URL for the given request, including path parameters.
    :param request: The request for which the URL is being constructed
    :return: A regex describing the URL path for the given request
    """
    logger.debug(f"Creating path regex for request: {request.path} [{request.method.name}]")

    path_regex = re.escape(request.path)

    for param in request.parameters:
        if param.location is not ParameterLocation.PATH:
            continue
        logger.debug(f"Processing path parameter: {param.name} | {param.location.name}")
        # Replace the placeholder in the path with a regex for either a value or string interpolation/concatenation.
        # The lambda function also prevents the backslash escapes in the replacement regex from being processed.
        path_regex = re.sub(
            fr"\\\{{{re.escape(re.escape(param.name))}\\\}}",
            lambda match: _create_val_or_var_regex(param.schema, True, is_trailing=match.end() == len(path_regex)),
            path_regex)

    return path_regex

    # The following lines add a query parameter suffix to the URL.
    # We don't use it for now because we want query parameters to be passed through the `params` argument.
    # query_params = [param for param in request.parameters if param.location is ParameterLocation.QUERY]
    # query_key_value_list_regex, _ = _create_key_value_list_regex(query_params, r"\?", "&", "", "=", "query")
    # return path_regex + query_key_value_list_regex


def _create_arguments_regex(request: Request) -> str:
    """
    Create a regex that describes the arguments passed to the Axios call, including request body and query/header parameters.
    :param request: The request for which to build the regex
    :return: A regex describing the call arguments, or an empty string if there is neither a body nor a config
    """
    logger.debug(f"Creating arguments regex for request: {request.path} [{request.method.name}]")

    # Build a regex for the request body
    body = request.request_body
    if body:  # only certain methods do have a request body
        logger.debug(f"Generating regex for request body")
        assert len(body.content) == 1 or all(content.schema == body.content[0].schema for content in body.content[1:]), \
            f"We assume there is always just one body, but we got {len(body.content)} different ones"

        content = body.content[0]

        if not body.required:
            logger.warning(f"Optional request body not supported: {request.method=}, {request.path=}")

        content_schema = content.schema
        if isinstance(content_schema, Object):
            body_key_value_list_regex, _ = _create_key_value_list_regex(
                content_schema.properties, "", r",\s*", r",?", r":\s*", "body", required_params=content_schema.required)
            data_regex = fr"(?:\{{\s*{body_key_value_list_regex}\s*\}}|(?&var))"
        else:
            data_regex = _create_val_or_var_regex(content_schema, False)

    elif request.method in (OperationMethod.PUT, OperationMethod.POST, OperationMethod.PATCH):
        # These methods require a request body, even if none is defined in the spec
        data_regex = r"(?:null|\{\})"
    else:
        data_regex = None

    # Build a regex for the header params
    header_params = [param for param in request.parameters if param.location is ParameterLocation.HEADER]
    if header_params:
        header_key_value_list_regex, has_required_header_params = _create_key_value_list_regex(
            header_params, "", r",\s*", r",?", r":\s*", "headers")
        header_regex = fr"headers:\s*\{{\s*{header_key_value_list_regex}\s*\}}"
        if not has_required_header_params:
            header_regex = fr"(?:{header_regex})?"
    else:
        header_regex = None
        has_required_header_params = False

    # Build a regex for the query params
    query_params = [param for param in request.parameters if param.location is ParameterLocation.QUERY]
    if query_params:
        query_key_value_list_regex, has_required_query_params = _create_key_value_list_regex(
            query_params, "", r",\s*", r",?", r":\s*", "params")
        query_regex = fr"params:\s*\{{\s*{query_key_value_list_regex}\s*\}}"
        if not has_required_query_params:
            query_regex = fr"(?:{query_regex})?"
    else:
        query_regex = None
        has_required_query_params = False

    # Combine header and query params to the complete config object
    if header_regex and query_regex:
        # The ",?" permits omitting the comma even if it's required, but we rely on the model to still get it right
        config_regex = fr"{header_regex},?\s*{query_regex}"
        has_required_config_params = has_required_header_params or has_required_query_params
    elif header_regex:
        config_regex = fr"{header_regex}"
        has_required_config_params = has_required_header_params
    elif query_regex:
        config_regex = fr"{query_regex}"
        has_required_config_params = has_required_query_params
    else:
        config_regex = None
        has_required_config_params = False

    if config_regex:
        config_regex = fr"\{{\s*{config_regex}\s*\}}"

    # Combine data and config to the complete argument list
    if data_regex and config_regex:
        if not has_required_config_params:
            arguments_regex = fr",\s*{data_regex}(?:,\s*{config_regex})?"
        else:
            arguments_regex = fr",\s*{data_regex},\s*{config_regex}"
    elif data_regex:
        arguments_regex = fr",\s*{data_regex}"
    elif config_regex:
        if not has_required_config_params:
            arguments_regex = fr"(?:,\s*{config_regex})?"
        else:
            arguments_regex = fr",\s*{config_regex}"
    else:
        arguments_regex = ""

    return arguments_regex


def _create_key_value_list_regex(params: list[Parameter] | list[Property], starter: str, separator: str,
                                 terminator: str, assignment: str, uid: str, required_params: list[str] | None = None,
                                 recursion_depth: int = 0) -> tuple[str, bool]:
    """
    Helper function to create a regex that matches a sequence of assignments from values to keys. The sequence may
    contain these key-value pairs in any order, but each pair at most once and required pairs exactly once.
    :param params: The items to be included in the sequence. The type of the values is extracted from their schemas
    :param starter: A prefix for the sequence (may be empty)
    :param separator: A separator between the key-value pairs
    :param terminator: A suffix for the sequence (may be empty)
    :param assignment: A separator between each key and value
    :param uid: A unique name for this sequence used to name a capturing group
    :param required_params: List of required parameters. Only needed if ``params`` contains Property objects
    :param recursion_depth: Current recursion depth
    :return: A regex matching a sequence of optional and required key-value assignments
    """
    if not params:
        return "", False

    guid = f"{uid}_{uuid.uuid4().hex[:8]}"
    separator_regex = fr"(?({guid}){separator}|(?P<{guid}>{starter}))"

    all_param_regexes = []

    for param in params:
        logger.debug(f"Processing parameter {param.name} | {_get_param_location(param)}")

        param_guid = f"{guid}_{_sanitize_name(param.name)}"
        param_name_regex = re.escape(param.name)
        param_name_regex = fr"""(?:{param_name_regex}|"{param_name_regex}"|'{param_name_regex}')"""
        param_regex = fr"""{param_name_regex}{assignment}{_create_val_or_var_regex(param.schema, False, recursion_depth=recursion_depth)}"""

        # The following regex reads as:
        # If the capture group belonging to the parameter is not defined, then define it and match the parameter.
        # If the capture group is already defined, then there is no match. This ensures that a parameter is only matched once.
        all_param_regexes.append(fr"(?({param_guid})|(?P<{param_guid}>{param_regex}))")

    all_param_regex = fr"(?:{separator_regex}{join_alternatives(all_param_regexes, inner_parentheses=False)})*"

    if required_params is not None:
        is_required = lambda param: param.name in required_params
    else:
        is_required = lambda param: param.required is not None and param.required

    required_param_regexes = []

    for param in params:
        if not is_required(param):
            continue
        logger.debug(f"Processing required parameter {param.name} | {_get_param_location(param)}")

        # (*FAIL) is an impossible match. So the following regex can be read as:
        # If the capture group belonging to the parameter is not defined, then backtrack immediately.
        required_param_regexes.append(fr"(?({guid}_{_sanitize_name(param.name)})|(*FAIL))")

    required_param_regex = "".join(required_param_regexes)

    has_required_params = len(required_param_regexes) > 0

    return f"{all_param_regex}{required_param_regex}{terminator}", has_required_params


def _sanitize_name(name: str) -> str:
    """
    Make sure no bad characters appear in a group name.
    :param name: The name to sanitize
    :return: A sanitized version of the name
    """
    return re.sub(r"\W", "_", name)


def _get_param_location(param: Parameter) -> str:
    """
    Helper function to get the location of a parameter as string.
    :param param: The Parameter object
    :return: The location as string
    """
    return param.location.name if hasattr(param, 'location') else "BODY"


def _create_val_or_var_regex(schema: Schema, in_url: bool, is_trailing: bool = False, allow_concat: bool = True,
                             recursion_depth: int = 0) -> str:
    """
    Helper function to create a regex that matches either a literal value or a variable.
    :param schema: The schema of the value (determines the data type)
    :param in_url: If true, variables are inserted through string interpolation or concatenation
    :param is_trailing: Whether this is a path parameter that comes right at the end of the URL
    :param allow_concat: Whether it is permitted to concatenate string literals to a string variable
    :param recursion_depth: Current recursion depth
    :return: A regex matching either a value or a variable
    """
    if in_url:
        # Actually, string interpolation is only allowed within backtick quotes, but we rely on the model to get this right.
        # To support concatenation, we surround the val/var expression with plus signs and quotation marks.
        # However, if the val/var is in a trailing position, we omit the plus and quote on the right side and instead
        # set a flag, which is later used to avoid enforcing a quotation mark at the end of the URL.
        suffix_regex = r"(?P<trailing_concat>)" if is_trailing else r" \+ (?&quote)"
        var_regex = fr"(?&str_var)|(?&quote) \+ {_create_val_or_var_regex(schema, False, allow_concat=False)}{suffix_regex}"
    else:
        # For string schemas, we also allow concatenating a string literal to the left and/or right of a variable
        var_regex = r"(?:(?&str) \+ )?(?&var)(?: \+ (?&str))?" \
            if isinstance(schema, String) and allow_concat else r"(?&var)"
    val_regex = _schema_to_regex(schema, in_url=in_url, recursion_depth=recursion_depth)
    return fr"(?:{val_regex}|{var_regex})"


def _schema_to_regex(schema: Schema, in_url: bool = False, recursion_depth: int = 0) -> str:
    """
    Create a regex that describes the instances of the datatype given by schema.
    :param schema: Schema of the datatype
    :param in_url: If true, regex describes a value inside a URL string
    :param recursion_depth: Current recursion depth
    :return: A regex describing instances of the datatype
    """
    logger.debug(f"Creating regex for schema type {schema.type.name}, {in_url=}")

    if schema.enum:
        enum_regexes = [re.escape(str(value)) for value in schema.enum if value is not None]
        enum_regex = join_alternatives(enum_regexes, inner_parentheses=False)
        return fr"(?&quote){enum_regex}(?&quote)" if isinstance(schema, String) else enum_regex

    if isinstance(schema, Integer):
        return r"(?&int)"

    if isinstance(schema, Number):
        return r"(?&num)"

    if isinstance(schema, String):
        return r"(?&url_str)" if in_url else r"(?&str)"

    if isinstance(schema, Boolean):
        return r"(?&bool)"

    if isinstance(schema, Array):
        if in_url:
            raise ValueError("Array parameters in URLs are not supported")
        return _create_array_regex(schema, recursion_depth=recursion_depth)

    if isinstance(schema, Object):
        if in_url:
            raise ValueError("Object parameters in URLs are not supported")
        return _create_object_regex(schema, recursion_depth=recursion_depth)

    if isinstance(schema, (OneOf, AnyOf)):
        schema_regexes = [_schema_to_regex(s, in_url=in_url, recursion_depth=recursion_depth) for s in schema.schemas]
        return join_alternatives(schema_regexes, inner_parentheses=False)

    raise ValueError(f"Unsupported {schema=}")


def _create_array_regex(schema: Array, recursion_depth: int = 0) -> str:
    """
    Create a regex that describes the instances of an array datatype.
    :param schema: Schema of the array datatype
    :param recursion_depth: Current recursion depth
    """
    if recursion_depth >= MAX_RECURSION_DEPTH:
        logger.warning("Maximum recursion depth exceeded, returning wildcard array regex")
        return r"\[[^[\]]*\]"  # allow everything except another nested array

    if schema.items is None:
        # From OAS 3.1 onward, omitting `items` is permitted and means items can have any type
        logger.info(f"Array schema without items: {schema}")
        return r"\[[^[\]]*\]"

    logger.debug(f"Creating regex for array schema with item type {schema.items.type.name}")

    if isinstance(schema.items, (Integer, Number, String, Boolean)):
        val_or_var_regex = _create_val_or_var_regex(schema.items, False, recursion_depth=recursion_depth + 1)
        content_regex = fr"\[\s*(?:{val_or_var_regex}(?:,\s*{val_or_var_regex})*)?\s*\]"
        return content_regex

    # To make objects with required and not-required attributes work, regex groups need to be instantiated with unique names.
    # Therefore, we cannot simply repeat the same pattern but need to instantiate a new one for each array item.
    content_regex = ""
    for i in range(MAX_OBJECTS_IN_ARRAY):
        leading_comma = r",\s*" if i < MAX_OBJECTS_IN_ARRAY - 1 else ""
        val_or_var_regex = _create_val_or_var_regex(schema.items, False, recursion_depth=recursion_depth + 1)
        content_regex = fr"(?:{leading_comma}{val_or_var_regex}{content_regex})?"

    return fr"\[\s*{content_regex}\s*\]"


def _create_object_regex(schema: Object, recursion_depth: int = 0) -> str:
    """
    Create a regex that describes the instances of an object datatype.
    :param schema: Schema of the object datatype
    :param recursion_depth: Current recursion depth
    """
    if recursion_depth >= MAX_RECURSION_DEPTH:
        logger.warning("Maximum recursion depth exceeded, returning wildcard object regex")
        return r"\{[^{}]*\}"  # allow everything except another nested object

    if not schema.properties:
        # From OAS 3.0 onward, omitting `properties` is permitted. Since our parser doesn't support `additionalProperties`,
        # we will assume it is true (its default value) and allow objects with arbitrary properties here.
        logger.info(f"Object schema without properties: {schema}")
        return r"\{[^{}]*\}"

    logger.debug(f"Creating regex for object schema with {len(schema.properties)} properties")

    key_value_list_regex, _ = _create_key_value_list_regex(
        schema.properties, "", r",\s*", r",?", r":\s*", "object",
        required_params=schema.required, recursion_depth=recursion_depth + 1)

    return fr"\{{\s*{key_value_list_regex}\s*\}}"


def join_alternatives(alternatives: list[str], inner_parentheses: bool = True, outer_parentheses: bool = True) -> str:
    """
    Join multiple regexes with an OR condition, making sure that parentheses are placed correctly.
    :param alternatives: The alternative regexes
    :param inner_parentheses: Whether to add parentheses around the individual alternatives
    :param outer_parentheses: Whether to add parentheses around the list of alternatives
    :return: A single regex matching any of the given alternatives
    """
    if len(alternatives) == 0:
        return ""

    if len(alternatives) == 1:
        return f"(?:{alternatives[0]})" if inner_parentheses and outer_parentheses else alternatives[0]

    if inner_parentheses:
        if outer_parentheses:
            return f"(?:(?:{')|(?:'.join(alternatives)}))"
        else:
            return f"(?:{')|(?:'.join(alternatives)})"
    else:
        if outer_parentheses:
            return f"(?:{'|'.join(alternatives)})"
        else:
            return f"{'|'.join(alternatives)}"


def validate_argument(arg_name: str, field_name: str, method: str, path: Path | None,
                      security_schemas: dict[str, Security]) -> bool:
    """
    Check if the given argument is a valid parameter for the given endpoint.
    :param arg_name: The name of the argument to check
    :param field_name: The field name in the Axios config ('headers', 'params', 'path_params', or 'data')
    :param method: The used HTTP method
    :param path: The Path object of the endpoint
    :param security_schemas: Allowed authentication schemes
    :return: Whether this argument is a valid parameter
    """
    # Certain header/security params are not explicitly listed in the spec but are still valid under certain conditions
    if field_name == 'headers' and arg_name == "Accept":
        return True
    if field_name == 'headers' and arg_name == "Content-Type":
        return method in ("put", "post", "patch")
    for security in security_schemas.values():
        if security.type is SecurityType.API_KEY:
            if field_name == location_to_field_name(security.location) and arg_name == security.name:
                return True
        elif security.type is SecurityType.HTTP or security.type is SecurityType.OAUTH2:
            if field_name == 'headers' and arg_name == "Authorization":
                return True
        elif security.type is SecurityType.OPEN_ID_CONNECT:
            logger.error("OpenID Connect is not supported - ignoring it")

    if path is None:
        return False

    if field_name == 'data':
        # Is the argument in the properties of this operation?
        for operation in path.operations:
            if operation.method.value == method:
                body = operation.request_body
                if body is None or not body.content:
                    break
                assert len(body.content) == 1 or all(
                    content.schema == body.content[0].schema for content in body.content[1:])
                assert isinstance(body.content[0].schema, Object)
                properties = body.content[0].schema.properties
                for property in properties:
                    if property.name == arg_name:
                        return True
                break

    else:
        def check_parameters(params: list[Parameter]) -> bool:
            """
            Check if the argument we are looking for is in this parameter list.
            :param params: List of parameters
            :return: If the argument in this function's closure is in `params`
            """
            for param in params:
                if param.name == arg_name:
                    if location_to_field_name(param.location) == field_name:
                        return True
                    break
            return False

        # Is the argument in the parameters of this path?
        if check_parameters(path.parameters):
            return True

        # Is the argument in the parameters of this operation?
        for operation in path.operations:
            if operation.method.value == method:
                if check_parameters(operation.parameters):
                    return True
                break

    return False


def find_path_in_spec(url: str, spec: Specification) -> tuple[list[Path] | None, Server | None]:
    """
    Try to find the path in the specification that corresponds to the given URL and return all matching candidates.
    If multiple paths match, order them so the one with the fewest path parameters and the longest URL comes first.
    :param url: The URL of the endpoint used for the request
    :param spec: The complete OpenAPI specification
    :return: List of Path objects and the Server object that correspond to ``url``
    """
    url_without_server, server = next(
        ((tmp, server) for server in spec.servers if (tmp := url.removeprefix(server.url)) != url), (None, None))

    if url_without_server is None:
        return None, None

    paths = []
    for path in spec.paths:
        path_regex, num_path_params = re.subn(r"\\{[^}]+\\}", r"[^/?&]+", re.escape(path.url))
        if re.fullmatch(path_regex, url_without_server):
            paths.append((num_path_params, path))

    if not paths:
        return None, None

    if len(paths) == 1:
        return [paths[0][1]], server

    # Order the list by num_path_params. The secondary sorting key (URL length) is mainly for the Google Sheets API,
    # where we need to make sure that the :<operation> suffix is interpreted as part of the path and not as a path param.
    paths.sort(key=lambda elem: (elem[0], -len(elem[1].url)))
    return list(tuple(zip(*paths))[1]), server  # remove num_path_params and return the paths as a list


def find_operation_in_path(method: str, path: Path) -> Operation | None:
    """
    Find the operation in the given path for the given method.
    :param method: The method
    :param path: The Path object
    :return: The Operation object that corresponds to ``method`` or ``None``
    """
    operations = [operation for operation in path.operations if operation.method.value == method]
    assert len(operations) <= 1, f"There should only be one operation for method {method} in path {path.url}"
    return operations[0] if operations else None


def location_to_field_name(location: ParameterLocation | BaseLocation) -> str:
    """
    Utility function to convert a ParameterLocation to the corresponding name/key in the Axios config.
    :param location: The location object
    :return: The corresponding field name
    """
    if location is ParameterLocation.HEADER or location is BaseLocation.HEADER:
        return "headers"
    if location is ParameterLocation.QUERY or location is BaseLocation.QUERY:
        return "params"
    if location is ParameterLocation.PATH:
        return "path_params"
    raise ValueError(f"Unexpected location {location}")


def field_name_to_location_str(field_name: str) -> str:
    """
    Utility function to convert a name/key in the Axios config to the *string value* of the corresponding
    ParameterLocation. Intended mainly for printing messages and thus also treats 'data' like a location.
    :param field_name: The field name
    :return: The string value of the corresponding location object
    """
    if field_name == "headers":
        return ParameterLocation.HEADER.value
    if field_name == "params":
        return ParameterLocation.QUERY.value
    if field_name == "path_params":
        return ParameterLocation.PATH.value
    if field_name == "data":
        return "request body"
    raise ValueError(f"Unexpected field name {field_name}")
