/*
 * capture_global_fetch.js — the CONTROL arm's one-line adaptation of the treatment's shim.
 *
 * THE PROBLEM. `wapiibench/capture_shim.js` is reused BYTE-FOR-BYTE by this arm, so both
 * conditions capture, normalize and coerce requests through exactly the same code. But that
 * file deliberately does NOT patch global `fetch`: it only publishes the capture function as
 * `globalThis.__wapiiCaptureFetch`, because the treatment injects it AS the generated
 * client's `fetch` option (`client.configure({ fetch: globalThis.__wapiiCaptureFetch })`).
 *
 * MEASURED, not assumed (control/verify_control.py reruns it): with the shim prepended and
 * nothing else, a hand-written `fetch('https://slack.com/api/...', {...})` call runs to
 * completion, exits 0 and writes NO config file — it goes to the real network layer and the
 * request is never captured. The control's answers are hand-written `fetch` calls, so the
 * treatment's wiring cannot capture them.
 *
 * THE ADAPTATION, in full. One assignment, appended after the shim and before the answer:
 *
 *     globalThis.fetch = globalThis.__wapiiCaptureFetch;
 *
 * Nothing in `wapiibench/capture_shim.js` changed. Normalization (mock.js parity), the
 * spec-declared type coercion, the `{index}_config.json` shape, the `_wapii_coercion` audit
 * block and the synthetic 200 response are all the treatment's code, unmodified, so a
 * captured control request and a captured treatment request are produced by the same lines.
 *
 * WHY GLOBAL PATCHING IS ACCEPTABLE HERE AND WAS NOT THERE. capture_shim.js's header explains
 * that it avoids monkeypatching global fetch because the generated runtime resolves
 * `config.fetch ?? fetch` and the option route is the exact seam. The control has no client
 * and no option to inject into: the model's own `fetch` identifier IS the seam, and the only
 * way to intercept it is to replace the binding it resolves to. `globalThis.fetch` is the
 * binding a bare `fetch(...)` resolves to in Node's CommonJS scope, so the assignment above
 * captures the model's call without the model knowing anything about capture — which is the
 * point: the answer must be an ordinary fetch call, not a call to a capture hook.
 *
 * CONSEQUENCE THE CONTRACT RELIES ON: an answer that calls `fetch` more than once still only
 * has its FIRST request captured (`_captured` in the shim), identical to the treatment.
 */

globalThis.fetch = globalThis.__wapiiCaptureFetch;
