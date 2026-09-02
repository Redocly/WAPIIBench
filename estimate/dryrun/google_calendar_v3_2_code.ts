// Get metadata for a calendar with calendar ID "<calendarId>".
import { client } from './client';
import { zodValidation } from './client.zod';
client.configure({ fetch: globalThis.__wapiiCaptureFetch, clientHeader: false });
client.use(zodValidation());

// DRY-RUN PLACEHOLDER C — hand-written by the scaffolding author, NOT by a generator agent.
// Intent: a plausible DISTRACTOR from the same five-operation whitelist (events.get instead
// of calendars.get), well-typed, right calendarId. Proves the five-operation whitelist can
// actually be chosen wrongly and that the endpoint verdict catches it.
client.auth.bearer('<token>');
client.calendar_events_get({
  path: { calendarId: "<calendarId>", eventId: "someEvent" },
});
