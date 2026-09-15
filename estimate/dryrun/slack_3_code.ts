// Restrict the app with ID "A87654321" for installation on the workspace with ID "T12345678". The request ID is "R23456789".
import { client } from './client';
import { zodValidation } from './client.zod';
client.configure({ fetch: globalThis.__wapiiCaptureFetch, clientHeader: false });
client.use(zodValidation());

// DRY-RUN PLACEHOLDER A — hand-written by the scaffolding author, NOT by a generator agent.
// Intent: a fully correct call, to prove the driver can produce a `correct` verdict.
//
// slack:3 (`admin.apps.restrict`) is NOT one of the 68 sampled tasks. It replaced slack:0
// (`admin.apps.approve`), which was, so this committed correct answer no longer belongs to a
// task any condition is scored on. The two operations are siblings with the identical
// three-field `URLSearchParams` body, so the fixture still exercises what it was written to
// exercise. `run_dryrun.assert_fixtures_outside_sample()` enforces the property.
client.auth.bearer('<token>');
client.admin_apps_restrict({
  body: new URLSearchParams({
    app_id: "A87654321",
    request_id: "R23456789",
    team_id: "T12345678",
  }),
});
