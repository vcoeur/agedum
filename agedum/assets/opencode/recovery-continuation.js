const CONTINUATION_REMINDER = "If this task resumes after a temporary provider-limit wait, continue the original task from its recorded state. Do not treat an earlier usage-limit error as a permanent block or as a review result. Reconcile unfinished worker work before retrying it; do not repeat completed side effects. This instruction does not authorize changing provider/model or bypassing permissions/review.";

function httpDateMilliseconds(value) {
  let match = /^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), (\d{2}) ([A-Za-z]{3}) (\d{4}) (\d{2}):(\d{2}):(\d{2}) GMT$/i.exec(value) ||
    /^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), (\d{2})-([A-Za-z]{3})-(\d{2}) (\d{2}):(\d{2}):(\d{2}) GMT$/i.exec(value);
  let fields;
  if (match) fields = match.slice(1);
  else {
    match = /^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) ([A-Za-z]{3}) ( [1-9]|\d{2}) (\d{2}):(\d{2}):(\d{2}) (\d{4})$/i.exec(value);
    if (!match) return NaN;
    fields = [match[2], match[1], match[6], match[3], match[4], match[5]];
  }
  const day = Number(fields[0]);
  const month = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"].indexOf(fields[1].toLowerCase());
  let year = Number(fields[2]);
  // Match the proxy's email.utils year pivot; asctime is UTC, never local time.
  if (year < 100) year += year < 69 ? 2000 : 1900;
  const hour = Number(fields[3]), minute = Number(fields[4]), second = Number(fields[5]);
  const date = new Date(Date.UTC(year, month, day, hour, minute, second));
  return date.getUTCFullYear() === year && date.getUTCMonth() === month &&
    date.getUTCDate() === day && date.getUTCHours() === hour &&
    date.getUTCMinutes() === minute && date.getUTCSeconds() === second ? date.getTime() : NaN;
}

export default {
  id: "agedum.recovery-continuation",
  async setup(ctx) {
    await ctx.session.hook("context", (event) => {
      event.system.push({ type: "text", text: CONTINUATION_REMINDER });
    });
    const options = ctx.options || {};
    if (!Array.isArray(options.retryMessages) || !options.retryMessages.length ||
        !options.retryMessages.every(value => typeof value === "string" && value.trim()) ||
        !Number.isFinite(options.maxWaitHours) || options.maxWaitHours <= 0) return;

    const phrases = options.retryMessages.map(value => value.toLowerCase());
    const cap = options.maxWaitHours * 3600;
    const active = new Map();
    const pending = new Map();
    const blocked = new Set();
    const keyOf = event => {
      const values = [event.sessionID, event.agent, event.model?.providerID, event.model?.id];
      return values.every(value => typeof value === "string" && value.length)
        ? JSON.stringify([...values, event.model.variant || ""]) : undefined;
    };
    const routed = event => {
      try {
        const url = new URL(event.request.url), base = new URL(options.baseURL);
        return url.origin === base.origin && Array.isArray(options.routes) &&
          options.routes.includes(event.model.providerID) &&
          url.pathname.startsWith(`/oc/${event.model.providerID}/`);
      } catch { return false; }
    };
    const invalidate = key => {
      pending.delete(key);
      active.delete(key);
      blocked.add(key);
    };
    await ctx.session.hook("http.request", event => {
      const key = keyOf(event);
      if (!key || blocked.has(key)) return;
      // Retry has no request ID/kind. Ambiguous overlap disables overrides for this key.
      if (active.has(key) || pending.has(key)) { invalidate(key); return; }
      active.set(key, {kind: event.kind, routed: routed(event)});
    });
    await ctx.session.hook("http.response", event => {
      const key = keyOf(event), request = active.get(key);
      pending.delete(key);
      active.delete(key);
      if (!key || blocked.has(key) || !request) return;
      if (request.kind !== event.kind) { invalidate(key); return; }
      if (!request.routed || !routed(event) || event.kind !== "primary" ||
          event.response.status !== 429 ||
          event.response.headers.get("x-agedum-retry-eligible") !== "1") return;
      const hint = event.response.headers.get("retry-after");
      let seconds;
      if (hint !== null && /^\s*\d+(?:\.\d+)?\s*$/.test(hint)) seconds = Number(hint);
      else if (hint !== null) seconds = Math.max(0, (httpDateMilliseconds(hint) - Date.now()) / 1000);
      if (!Number.isFinite(seconds * 1000) || seconds < 0 || seconds > cap) return;
      pending.set(key, {delay: seconds * 1000, observed: Date.now()});
    });
    await ctx.session.hook("retry", event => {
      const key = keyOf(event), marker = pending.get(key);
      pending.delete(key);
      // A transport failure without a response also makes later correlation unsafe.
      if (active.has(key)) { invalidate(key); return; }
      if (!marker || blocked.has(key) || Date.now() - marker.observed > 30_000 ||
          event.error.status !== 429) return;
      const message = event.error.message.toLowerCase();
      if (/auth|billing|config|invalid|permission|forbidden/.test(event.error.type || "")) return;
      if (/billing|balance|arrears|payment|credit|insufficient[_ ]quota|insufficient funds|auth|api[_ ]key|invalid|credential|expired|access denied|not found|unsupported|config|permission|forbidden|account suspended/.test(message) ||
          !phrases.some(phrase => message.includes(phrase))) return;
      event.decision = {retry: true, delay: marker.delay};
    });
  },
};
