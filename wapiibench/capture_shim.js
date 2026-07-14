/*
 * capture_shim.js — request capture for the WAPIIBench "sdk-repair" arm.
 *
 * PURPOSE
 * -------
 * The Redocly `generate-client` output (redocly-cli PR #2885, branch feat/ts-client-gen)
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
 * VERIFIED CALL SHAPE (redocly-cli@f5776cf, packages/client-generator/src/runtime/send.ts)
 * ----------------------------------------------------------------------------------------
 *   doFetch(url, init) is called with:
 *     url        : string, with the query string ALREADY baked in (url.ts serializes it)
 *     init.method: string ("GET", "POST", ...)
 *     init.headers: a plain Record<string,string> (NOT a Headers instance); default
 *                   `Accept: application/json`, plus `Content-Type: application/json` added
 *                   when a JSON body is serialized
 *     init.body  : ALREADY serialized -> a JSON string (JSON.stringify) for JSON bodies, or
 *                   a URLSearchParams / FormData / Blob / ArrayBuffer / string for others
 *   (config.fetch is typed `fetch?: typeof fetch` on ClientConfig — runtime/types.ts.)
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
      writeConfig(config);
    }
  } catch (e) {
    // Match execute()'s failure contract: a config file with an ERROR key mapped by the
    // Python side to Verdict.EXECUTION_ERROR (evaluation.py:420). Same literal value, so no
    // scoring code changes are needed.
    writeConfig({ ERROR: 'execution_error', _detail: String((e && e.message) || e) });
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

module.exports = { captureFetch, headersToObject, stripQueryIntoParams, normalizeData };
