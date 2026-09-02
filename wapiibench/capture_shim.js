/*
 * capture_shim.js — request capture for the WAPIIBench "sdk-repair" arm.
 *
 * PURPOSE
 * -------
 * The Redocly `generate-client` output (released @redocly/cli, pinned 2.51.0)
 * defaults to a *web-standard fetch* runtime (the `inline` runtime), NOT axios. WAPIIBench's
 * existing capture asset, wapiibench/mock.js, installs axios-mock-adapter on the axios
 * singleton and writes `{index}_config.json`; it never fires for a fetch-based client.
 *
 * This file is the fetch-layer equivalent. Rather than monkeypatching the global `fetch`
 * (which the generated runtime would only pick up incidentally), it exposes a capture
 * function that is injected AS the client's `fetch` option. The generated runtime resolves
 * its fetch via `const doFetch = config.fetch ?? fetch;` (send.ts) and calls it as
 *     doFetch(context.url, { ...fetchInit, method, headers, body: payload })
 * so a `fetch`-compatible function set through `createClient({ fetch })` or
 * `configure({ fetch })` receives every outbound request. See sdk_repair_arm.py for how the
 * starter code wires it in, and the branch README for the verification of this seam.
 *
 * VERIFIED CALL SHAPE (released @redocly/cli 2.51.0; source at redocly-cli HEAD 2566393,
 *                      packages/client-generator/src/generators/typescript/runtime/send.ts:138)
 * ----------------------------------------------------------------------------------------
 *   doFetch(url, init) is called with:
 *     url        : string, with the query string ALREADY baked in (url.ts serializes it)
 *     init.method: string ("GET", "POST", ...)
 *     init.headers: a plain Record<string,string> (NOT a Headers instance); default
 *                   `Accept: application/json`, plus `Content-Type: application/json` added
 *                   when a JSON body is serialized
 *     init.body  : ALREADY serialized -> a JSON string (JSON.stringify) for JSON bodies, or
 *                   a URLSearchParams / FormData / Blob / ArrayBuffer / string for others
 *   (config.fetch is typed `fetch?: typeof fetch;` on ClientConfig —
 *    packages/client-generator/src/generators/typescript/runtime/types.ts:105.)
 *
 * OUTPUT SHAPE (verified against redocly/wapiibench@main)
 * ------------------------------------------------------
 * compare()/_compare_configs() (evaluation.py:474/:566) reads exactly these top-level keys
 * off the logged config; every other key is explicitly ignored (evaluation.py:645):
 *   - url     : string, query string STRIPPED (decodeURIComponent'd)         mock.js:7-12
 *   - method  : string, LOWERCASE ("get","post",...)  — ground truth is lowercase (:605)
 *   - params  : object, folded from the query string; ''->true               mock.js:13-28
 *   - data    : object, JSON body parsed, or urlencoded/FormData parsed       mock.js:30-81
 *   - headers : object
 * On failure the file is `{"ERROR": "<verdict>"}` (compare handles that at :578). We emit
 * `{"ERROR": "execution_error"}` to match Verdict.EXECUTION_ERROR (evaluation.py:101/420).
 * `path_params` is NOT written here — compare() derives it via _add_path_params (:516).
 * One extra key is written: `_wapii_coercion`, the audit trail of the spec-driven query
 * value type coercion below (which parameter was coerced to what, and why any was left as a
 * string). It is not in evaluation.FIELD_KEYS, so scoring ignores it.
 *
 * The query/body normalization below is a faithful port of mock.js:6-88 so captured values
 * compare byte-for-byte equal to the axios arms under _compare_configs value equality.
 */

const fs = require('fs');

// The output path. execute()-style printf substitution fills this in, mirroring the way
// mock.js has its single placeholder replaced with the config path via the same
// (template, config_log_file) string substitution used for mock.js (evaluation.py:409).
// This file therefore contains exactly ONE placeholder (below), like mock.js. An env
// override is provided for standalone / ts-node runs where no substitution happens.
const CONFIG_LOG_FILE = process.env.WAPII_CONFIG_OUT || '%s';

// --- shared normalization (ported verbatim in behavior from mock.js) ---------------

function stripQueryIntoParams(config, rawUrl) {
  const url = decodeURIComponent(rawUrl);
  const queryIndex = url.indexOf('?');
  if (queryIndex === -1) {
    config.url = url;
    return;
  }
  config.url = url.slice(0, queryIndex);
  const query = url.slice(queryIndex + 1);
  const keyValuePairs = new URLSearchParams(query);
  if (!Object.hasOwn(config, 'params') || config.params == null) {
    config.params = {};
  }
  for (const [key, value] of keyValuePairs) {
    if (Object.hasOwn(config.params, key)) {
      console.warn(`Duplicate query parameter '${key}' - overwriting value '${config.params[key]}' with '${value}'`);
    }
    if (value !== '') {
      config.params[key] = value;
    } else {
      console.warn(`No value for '${key}' - assuming value 'true'`);
      config.params[key] = true;
    }
  }
}

// Port of mock.js:30-81. `data` reaches us pre-serialized: a JSON string, a urlencoded
// string, FormData, or (defensively) an already-parsed object.
function normalizeData(config, data) {
  if (data === undefined || data === null) {
    return;
  }
  if (!data || data === 'null') {
    config.data = {};
  } else if (typeof data === 'string' && data.length > 0) {
    if (data.startsWith('{')) {
      config.data = JSON.parse(data);
    } else {
      config.data = {};
      data = decodeURIComponent(data.replaceAll('+', ' '));
      const keyValuePairs = new URLSearchParams(data);
      for (let [key, value] of keyValuePairs) {
        if (key.endsWith('[]')) {
          key = key.slice(0, -2);
          if (config.data[key]) {
            config.data[key].push(value);
          } else {
            config.data[key] = [value];
          }
        } else if (key.endsWith(']')) {
          const matches = key.matchAll(/([^[\]]+)/g);
          let tmp = config.data;
          let lastKey;
          let nextKey;
          for (const match of matches) {
            nextKey = match[1];
            if (lastKey) {
              if (!tmp[lastKey]) {
                tmp[lastKey] = !isNaN(parseInt(nextKey)) ? [] : {};
              }
              tmp = tmp[lastKey];
            }
            lastKey = nextKey;
          }
          tmp[lastKey] = value;
        } else {
          config.data[key] = value;
        }
      }
    }
  } else if (typeof data === 'object' && data.constructor
      && data.constructor.name === 'URLSearchParams') {
    // A URLSearchParams body -> treat its string form as urlencoded.
    normalizeData(config, data.toString());
  } else if (typeof data === 'object' && data.constructor
      && data.constructor.name === 'FormData') {
    config.data = {};
    if (Array.isArray(data._streams)) {           // axios/form-data internal shape (mock.js:70-77)
      const formData = data._streams;
      for (let i = 0; i < formData.length; i += 3) {
        const key = formData[i].match(/name="(.*?)"/)[1];
        config.data[key] = formData[i + 1];
      }
    } else if (typeof data.forEach === 'function') { // WHATWG FormData
      data.forEach((value, key) => { config.data[key] = value; });
    }
  } else if (typeof data === 'object') {
    // Defensive: a plain object body (should not happen — send.ts serializes JSON bodies
    // to a string before calling fetch — but harmless if it ever does).
    config.data = data;
  } else {
    console.warn(`Unknown type of data '${data}'`);
  }
}

// Headers -> plain object.
// The generated runtime passes headers as a plain Record<string,string> (send.ts), but we
// also handle a Headers instance / entry array defensively.
// HEADER-CASING NOTE (fairness): a WHATWG `Headers` object lowercases names. The generated
// runtime does NOT use Headers on the outbound path — it forwards the plain record the
// caller built — so canonical casing is preserved as written. We still restore casing for a
// known set in case a caller (or a future runtime change) routes through Headers.
// `Accept` / `Content-Type` are ignored by scoring anyway (SPECIAL_KEYS, evaluation.py:93);
// `Authorization` matters (lowercased "authorization" would score MISSING_KEY vs expected
// "Authorization"). Verify each API's ground-truth header casing before relying on this map.
const HEADER_CASING = {
  'authorization': 'Authorization',
  'accept': 'Accept',
  'content-type': 'Content-Type',
};
function headersToObject(headers) {
  const out = {};
  const assign = (k, v) => { out[HEADER_CASING[k.toLowerCase()] || k] = v; };
  if (!headers) return out;
  if (typeof headers.forEach === 'function' && !Array.isArray(headers)) {
    headers.forEach((value, key) => assign(key, value)); // Headers or Map
  } else if (Array.isArray(headers)) {
    for (const [k, v] of headers) assign(k, v);
  } else if (typeof headers === 'object') {
    for (const k of Object.keys(headers)) assign(k, headers[k]);
  }
  return out;
}

function writeConfig(config) {
  // Written synchronously so the file exists before the process exits, even though the
  // client resolves our synthetic response immediately (the snippet may not await it).
  fs.writeFileSync(CONFIG_LOG_FILE, JSON.stringify(config, null, 2));
}

// --- SPEC-DRIVEN type coercion of captured query values ------------------------------
//
// WHY: mock.js reads `config.params` off the axios config object, so an axios answer keeps
// the JavaScript type the model wrote (`{ params: { limit: 100 } }` is logged as the NUMBER
// 100). A generated fetch client serializes every parameter into the URL, so all we can
// recover above is the STRING "100" -- and `_compare_configs` compares with
// `actual_value == expected_value`, so a perfectly correct SDK answer scores
// INCORRECT_VALUE. Measured on the datasets: 51 of 395 synthetic tasks and 2 of 28
// real-world tasks have a non-string expected query value.
//
// THE FIX: put the type back using the type THE OPENAPI DESCRIPTION DECLARES for that
// parameter. The declared types arrive in `wapii_param_types.json`, built by
// `sdk_repair_arm.param_types_from_spec()` from `parameters[].schema.type` of the API's spec
// and NOTHING else -- no dataset, no task, no expected config. That is the load-bearing
// property: coercing towards the expected value instead of towards the spec would let a
// WRONG value be reshaped into a matching one, and the benchmark would be scoring itself.
// So the only question this code ever asks is "what does the spec say this parameter is",
// never "what would make this compare equal".
//
// WHAT IS COERCED (and only this):
//   integer / number -> Number, when the captured text really is a numeric literal
//   boolean          -> true / false, for exactly the texts "true" and "false"
//   array            -> rebuilt from the query string per the parameter's style/explode,
//                       then each item coerced by its declared item type
//   string           -> left exactly as captured
//   anything else, no declared type, or an unresolvable union -> left as the captured
//                       string, with the reason recorded in `_wapii_coercion`
//
// WHAT IS NOT TOUCHED:
//   * request BODIES. A JSON body is parsed by JSON.parse and already carries real types; a
//     urlencoded body is string-typed on BOTH sides of the comparison (mock.js parses it the
//     same way for the axios arms), so coercing it would introduce a NEW asymmetry.
//   * `path_params`. `evaluation._add_path_params` re-derives that key by regexing the URL,
//     for the expected and the captured config alike, so both sides are always strings; a
//     typed value written here would be overwritten in the normal case and would compare
//     against a string in the abnormal one. Measured: 0 of 395 synthetic and 0 of 28
//     real-world expected path_params values are non-string, so there is nothing to fix.
//     The declared path types ARE reported under `_wapii_coercion.path_values` for
//     transparency, without being applied.
//   * `evaluation.py`. The comparison still demands exact equality and the dataset keeps its
//     original typed values -- loosening either would rescore every published arm.
//
// Everything the pass did, and every value it declined to touch, is recorded in the captured
// config under `_wapii_coercion`. That key is not one of evaluation.FIELD_KEYS, so scoring
// ignores it (evaluation.py's "parameters present that are not expected" loop skips any
// top-level key outside FIELD_KEYS).

const path = require('path');

// Written next to the generated client by sdk_repair_arm.write_param_types(); the shim runs
// with cwd = that client dir (see execute_sdk_repair), hence the bare relative name.
const PARAM_TYPES_FILE = process.env.WAPII_PARAM_TYPES || 'wapii_param_types.json';

function loadSpecParamTypes() {
  try {
    return JSON.parse(fs.readFileSync(path.resolve(PARAM_TYPES_FILE), 'utf8'));
  } catch (e) {
    return null;   // absent or unreadable -> coerce nothing, and say so in the diagnostics
  }
}

// server + path template -> a matcher, with the path parameter names in capture order.
// Same shape of rule as the Python side uses to resolve a URL against the spec: escape the
// literal text, then turn each {placeholder} into a single non-slash segment.
function pathMatcher(server, template) {
  const names = [];
  const literal = String(server).replace(/\/+$/, '') + String(template);
  let pattern = literal.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  pattern = pattern.replace(/\\\{([^}]*)\\\}/g, (_m, name) => {
    names.push(name.replace(/\\(.)/g, '$1'));
    return '([^/]+)';
  });
  return { regex: new RegExp('^' + pattern + '/?$'), names };
}

// Which spec operation the captured request hit. Ties are broken towards the MOST LITERAL
// path (fewest templated segments), so a concrete path never loses to a template that also
// happens to match.
function matchOperation(table, method, urlWithoutQuery) {
  let best = null;
  for (const record of (table.operations || [])) {
    if (String(record.method).toLowerCase() !== String(method).toLowerCase()) continue;
    for (const server of (record.servers || [])) {
      const matcher = pathMatcher(server, record.path);
      const found = matcher.regex.exec(urlWithoutQuery);
      if (!found) continue;
      const templated = (String(record.path).match(/\{/g) || []).length;
      const score = templated * 1000 - String(record.path).length;
      if (best === null || score < best.score) {
        const pathValues = {};
        matcher.names.forEach((name, i) => { pathValues[name] = found[i + 1]; });
        best = { record, pathValues, score };
      }
      break;
    }
  }
  return best;
}

function coerceScalar(raw, declaredType) {
  if (typeof raw !== 'string') {
    return { ok: false, reason: 'captured value is not a string' };
  }
  const text = raw.trim();
  if (declaredType === 'integer') {
    if (/^[+-]?\d+$/.test(text) && Number.isSafeInteger(Number(text))) {
      return { ok: true, value: Number(text) };
    }
    return { ok: false, reason: 'spec declares integer, captured text is not an integer literal' };
  }
  if (declaredType === 'number') {
    if (text !== '' && Number.isFinite(Number(text))) {
      return { ok: true, value: Number(text) };
    }
    return { ok: false, reason: 'spec declares number, captured text is not a numeric literal' };
  }
  if (declaredType === 'boolean') {
    if (text === 'true') return { ok: true, value: true };
    if (text === 'false') return { ok: true, value: false };
    return { ok: false, reason: 'spec declares boolean, captured text is neither true nor false' };
  }
  if (declaredType === 'string') {
    return { ok: false, reason: 'spec declares string, left as captured' };
  }
  if (!declaredType) {
    return { ok: false, reason: 'spec declares no resolvable type, left as captured' };
  }
  return { ok: false, reason: 'no coercion defined for declared type ' + declaredType };
}

// An array parameter's wire form depends on its serialization: `explode` (the OpenAPI
// default for style `form`) repeats the key, otherwise the values are joined with the
// style's delimiter.
function coerceArray(rawValues, entry) {
  const style = entry.style || 'form';
  const explode = entry.explode !== false;
  let items = [];
  if (explode) {
    items = rawValues.slice();
  } else {
    const delimiter = style === 'spaceDelimited' ? ' ' : (style === 'pipeDelimited' ? '|' : ',');
    for (const value of rawValues) {
      for (const part of String(value).split(delimiter)) items.push(part);
    }
  }
  const out = [];
  const notes = [];
  // A declared string item needs no coercion, so only a FAILED coercion of a numeric or
  // boolean item is worth recording here.
  const coercibleItem = ['integer', 'number', 'boolean'].includes(entry.items_declared_type);
  for (const item of items) {
    const coerced = coerceScalar(item, entry.items_declared_type);
    out.push(coerced.ok ? coerced.value : item);
    if (!coerced.ok && coercibleItem) notes.push(coerced.reason);
  }
  return { value: out, notes };
}

// The query string as ordered pairs, decoded exactly the way stripQueryIntoParams decodes it
// (whole-URL decodeURIComponent, mock.js parity), so keys and values line up with
// config.params. Needed because an exploded array repeats its key and the params object can
// only keep one value per key.
function rawQueryPairs(rawUrl) {
  const url = decodeURIComponent(rawUrl);
  const queryIndex = url.indexOf('?');
  if (queryIndex === -1) return [];
  return Array.from(new URLSearchParams(url.slice(queryIndex + 1)));
}

function coerceParamsFromSpecDeclaredTypes(config, rawUrl) {
  const diagnostics = {
    param_types_file: PARAM_TYPES_FILE,
    type_source: 'openapi spec parameter schemas (never the expected config)',
    coerced: {},
    left_as_string: {},
  };
  const table = loadSpecParamTypes();
  if (!table) {
    diagnostics.skipped = 'no ' + PARAM_TYPES_FILE + ' beside the client: nothing coerced, '
      + 'every captured query value stays a string';
    config._wapii_coercion = diagnostics;
    return;
  }
  diagnostics.type_source = table.source || diagnostics.type_source;
  diagnostics.spec = table.spec;

  const matched = matchOperation(table, config.method, config.url);
  if (matched === null) {
    diagnostics.skipped = 'the captured method plus URL matched no operation in the spec '
      + 'table, so no parameter has a declared type here';
    config._wapii_coercion = diagnostics;
    return;
  }
  diagnostics.operation_id = matched.record.operation_id;
  diagnostics.operation_path = matched.record.path;

  const declared = matched.record.query || {};
  const pairs = rawQueryPairs(rawUrl);
  const params = config.params || {};
  for (const key of Object.keys(params)) {
    const entry = declared[key];
    if (!entry) {
      diagnostics.left_as_string[key] = 'not declared as a query parameter of this operation';
      continue;
    }
    if (typeof params[key] !== 'string') {
      diagnostics.left_as_string[key] = 'captured value is already non-string '
        + '(the empty-value rule above made it boolean true)';
      continue;
    }
    if (entry.declared_type === 'array') {
      const values = pairs.filter((pair) => pair[0] === key).map((pair) => pair[1]);
      const coerced = coerceArray(values.length ? values : [params[key]], entry);
      params[key] = coerced.value;
      diagnostics.coerced[key] = {
        declared_type: 'array',
        items_declared_type: entry.items_declared_type || null,
        style: entry.style, explode: entry.explode !== false, value: coerced.value,
      };
      if (coerced.notes.length) diagnostics.coerced[key].item_notes = coerced.notes;
      continue;
    }
    const coerced = coerceScalar(params[key], entry.declared_type);
    if (coerced.ok) {
      params[key] = coerced.value;
      diagnostics.coerced[key] = { declared_type: entry.declared_type, value: coerced.value };
    } else {
      diagnostics.left_as_string[key] = entry.unresolved_reason || coerced.reason;
    }
  }

  // Path values: REPORTED, never applied -- see the header note. _add_path_params re-derives
  // path_params as strings from the URL on both sides of the comparison.
  const pathValues = {};
  for (const name of Object.keys(matched.pathValues)) {
    const entry = (matched.record.path_params || {})[name] || {};
    pathValues[name] = {
      captured: matched.pathValues[name],
      declared_type: entry.declared_type || null,
      applied: false,
      why_not_applied: 'evaluation._add_path_params re-derives path_params from the URL as '
        + 'strings for the expected config too, so both sides are strings by construction',
    };
  }
  if (Object.keys(pathValues).length) diagnostics.path_values = pathValues;

  config._wapii_coercion = diagnostics;
}

// --- the capture function injected as the client's `fetch` option ---------------------
//
// Signature-compatible with global fetch: captureFetch(input, init). The generated runtime
// always calls it as doFetch(url, init) with a string url (send.ts), but we also accept a
// Request instance for robustness. On the first captured request we write the config and
// short-circuit the network with a synthetic 200 (mirroring mock.js `return [200]`).
let _captured = false;
function captureFetch(input, init) {
  try {
    let rawUrl, method, headers, body;
    if (typeof input === 'object' && input && typeof input.url === 'string' && input.method) {
      // Request instance (defensive — the generated runtime does not use this form).
      rawUrl = input.url;
      method = (init && init.method) || input.method;
      headers = (init && init.headers) || input.headers;
      body = (init && init.body !== undefined) ? init.body : undefined;
    } else {
      rawUrl = String(input);
      method = (init && init.method) || 'GET';
      headers = init && init.headers;
      body = init && init.body;
    }

    // Only the FIRST request is scored (WAPIIBench tasks are single-call). Capture it once.
    if (!_captured) {
      _captured = true;
      const config = {};
      config.method = String(method).toLowerCase();       // ground truth is lowercase
      config.headers = headersToObject(headers);
      stripQueryIntoParams(config, rawUrl);
      normalizeData(config, body);
      // Put back the types the URL threw away, using the SPEC's declared parameter types.
      coerceParamsFromSpecDeclaredTypes(config, rawUrl);
      writeConfig(config);
    }
  } catch (e) {
    // Match execute()'s failure contract: a config file with an ERROR key mapped by the
    // Python side to Verdict.EXECUTION_ERROR (evaluation.py:420).
    // CASING IS LOAD-BEARING: Verdict is a strenum.StrEnum whose members use auto(), so the
    // VALUE equals the member NAME -- Verdict.EXECUTION_ERROR is the string
    // 'EXECUTION_ERROR', not 'execution_error'. Writing the lowercase form here makes
    // _analyze_sample() fall through its verdict chain and
    // `raise AssertionError(f"Unexpected verdict {sample['ERROR']}")`, crashing analyze()
    // for the whole run instead of scoring the sample as nonexecutable.
    writeConfig({ ERROR: 'EXECUTION_ERROR', _detail: String((e && e.message) || e) });
  }
  // Synthetic OK response so the client resolves without hitting the network. `application/
  // json` + `{}` body keeps the generated runtime's `response.json()` parse path happy.
  return Promise.resolve(
    new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } }));
}

// Expose the capture function to the executed snippet WITHOUT patching global fetch. The
// starter code (SETUPS 'sdk-*' in evaluation.py) injects it via the client's fetch option:
//   configure({ fetch: globalThis.__wapiiCaptureFetch })   // default client + free fns
//   createClient({ fetch: globalThis.__wapiiCaptureFetch }) // per-instance
globalThis.__wapiiCaptureFetch = captureFetch;

module.exports = { captureFetch, headersToObject, stripQueryIntoParams, normalizeData,
  coerceParamsFromSpecDeclaredTypes, coerceScalar, coerceArray, matchOperation,
  loadSpecParamTypes };
