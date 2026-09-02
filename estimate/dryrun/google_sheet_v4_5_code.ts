// Retrieve the values from range A1:B2 in the spreadsheet with ID "<spreadsheetId>".
import { client } from './client';
import { zodValidation } from './client.zod';
client.configure({ fetch: globalThis.__wapiiCaptureFetch, clientHeader: false });
client.use(zodValidation());

// DRY-RUN PLACEHOLDER B — hand-written by the scaffolding author, NOT by a generator agent.
// Intent: the RIGHT operation, a well-typed call that `tsc` accepts happily, and entirely
// WRONG VALUES. This is the case the coordinator's six-variant typecheck test showed tsc
// cannot catch, so the scoring driver has to. Expected: NOT correct.
client.auth.bearer('<token>');
client.sheets_spreadsheets_values_get({
  path: { spreadsheetId: "1zzzWRONGwrongWRONG", range: "Z99:AA100" },
});
