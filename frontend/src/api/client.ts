/**
 * Typed HTTP client for the GreenCharge backend.
 *
 * Every URL is relative ("/api/..."): Vite proxies /api to the FastAPI server, so the
 * dashboard never hard-codes a host. Every request carries an AbortSignal timeout — 10 s, or
 * `LLM_REQUEST_TIMEOUT_MS` for the two endpoints that wait on a model — and throws `ApiError`
 * on a non-2xx response or a network failure.
 *
 * The response types below mirror the live Phase 4, Phase 6 and Phase 7 API shapes field for
 * field. Do not add a field the backend does not send.
 */

/**
 * Request timeout. The slowest endpoints re-tick the optimizer (POST /api/optimizer/weights) or
 * wait on the charge point (POST /api/debug/plug-in, whose own server-side budget is 15 s — a
 * plug-in that hangs surfaces here as a network-error timeout rather than the server's 504).
 */
export const REQUEST_TIMEOUT_MS = 10_000;

/**
 * The budget for the two Phase 7 endpoints that wait on a model provider. It is a hard
 * client-side abort rather than an open-ended wait, because neither call is allowed to strand a
 * driver: past it the plug-in form is simply filled in by hand and the plan screen keeps the
 * deterministic explanation it renders anyway.
 */
export const LLM_REQUEST_TIMEOUT_MS = 15_000;

/**
 * The budget for POST /api/demo/reset. Its own server-side work is bounded but slower than a
 * read: up to 3 s waiting for the StopTransactions, up to 3 s for an orchestrator tick still in
 * flight, then a TRUNCATE that retries a lock timeout twice. Ten seconds would abort a reset that
 * was still going to succeed, and on stage a reset that reports failure while working is worse
 * than one that takes a moment.
 */
export const DEMO_RESET_TIMEOUT_MS = 20_000;

/** `ApiError.status` when the request never produced an HTTP response (offline, DNS, timeout). */
export const NETWORK_ERROR_STATUS = 0;

/* ------------------------------------------------------------------ response types */

/** GET /api/clock — the simulation clock. `now` is ISO-8601 with offset; never the browser clock. */
export type ClockInfo = {
  now: string;
  time_scale: number;
};

/** GET /api/grid/latest, GET /api/grid/forecast. `source` is "estimated" for the synthetic provider. */
export type CarbonPoint = {
  ts: string;
  carbon_intensity: number;
  renewable_pct: number | null;
  fossil_pct: number | null;
  source: string;
};

export type Charger = {
  id: number;
  site_id: number;
  ocpp_id: string;
  max_power_kw: number;
  status: string;
  last_heartbeat: string | null;
};

export type Site = {
  id: number;
  name: string;
  latitude: number;
  longitude: number;
  grid_zone: string;
  max_power_kw: number;
  demand_charge_inr_per_kva: number;
  chargers: Charger[];
};

/** One 15-minute planned slot. 96 of them make a horizon. */
export type ScheduleSlot = {
  slot_start: string;
  power_kw: number;
};

/** GET /api/sessions/active. `schedule` holds 96 slots, or [] before the first tick. */
export type ActiveSession = {
  id: number;
  charger_id: number;
  ocpp_id: string;
  ocpp_transaction_id: number | null;
  vehicle_model: string;
  battery_kwh: number;
  max_charge_kw: number;
  soc_start: number;
  soc_target: number;
  soc_current: number;
  plugged_in_at: string;
  deadline: string;
  energy_delivered_kwh: number;
  co2_actual_g: number;
  co2_baseline_g: number;
  cost_actual_inr: number;
  cost_baseline_inr: number;
  status: string;
  manual_limit_w: number | null;
  projected_unmet_kwh: number | null;
  on_time: boolean;
  schedule: ScheduleSlot[];
};

/** GET /api/sessions/{id}/schedule */
export type SessionSchedule = {
  session_id: number;
  computed_at: string;
  slots: ScheduleSlot[];
};

export type LoadCurveSlot = {
  slot_start: string;
  optimized_kw: number;
  baseline_kw: number;
  is_past: boolean;
};

/** GET /api/sites/{id}/load-curve */
export type LoadCurve = {
  site_id: number;
  max_power_kw: number;
  window_start: string;
  now: string;
  optimized_peak_kw: number;
  baseline_peak_kw: number;
  slots: LoadCurveSlot[];
};

/** GET /api/impact/summary */
export type ImpactSummary = {
  co2_saved_kg: number;
  cost_saved_inr: number;
  sessions_on_time: number;
  total_sessions: number;
};

/** GET /api/ocpp/log — raw OCPP-J frames, newest first. */
export type OcppFrame = {
  ts: string;
  direction: 'in' | 'out';
  ocpp_id: string;
  frame: string;
};

/** The orchestrator tick returned by POST /api/optimizer/weights. Keys of `unmet_kwh` are session ids. */
export type TickInfo = {
  computed_at: string;
  reason: string;
  n_sessions: number;
  status: string;
  solve_ms: number;
  unmet_kwh: Record<string, number>;
};

/** POST /api/optimizer/weights */
export type WeightsResponse = {
  alpha: number;
  beta: number;
  tick: TickInfo;
};

/** POST /api/sessions/{id}/override */
export type OverrideResponse = {
  session_id: number;
  limit_w: number;
  status: string;
};

/** GET /api/vehicles — the catalogue from data/vehicles.json. The driver form never invents car data. */
export type Vehicle = {
  model: string;
  battery_kwh: number;
  max_ac_kw: number;
  max_dc_kw: number;
};

/**
 * GET /api/sessions/{id}/impact — one session's own savings, ETA and green/grey split.
 * `eta` is null when the current plan never covers the remaining need.
 * green/grey: metered energy delivered in slots whose CI is below the mean CI of the session's
 * horizon counts as green — a presentation choice, not a measurement, and the UI says so.
 */
export type SessionImpact = {
  session_id: number;
  co2_saved_g: number;
  cost_saved_inr: number;
  co2_actual_g: number;
  co2_baseline_g: number;
  green_kwh: number;
  grey_kwh: number;
  eta: string | null;
  projected_soc_at_deadline: number;
  on_time: boolean;
  energy_needed_kwh: number;
  energy_delivered_kwh: number;
};

/**
 * GET /api/sessions/{id}/override-preview — what "I'm leaving now" costs against the current plan,
 * shown before the driver confirms. `limit_w` is the full power the override would command.
 * Either ETA is null when that path never reaches the target.
 */
export type OverridePreview = {
  session_id: number;
  extra_co2_g: number;
  extra_cost_inr: number;
  eta_now: string | null;
  eta_planned: string | null;
  limit_w: number;
};

/** POST /api/debug/plug-in body. `soc_start`/`soc_target` are 0..1 fractions, target above start. */
export type PlugInRequest = {
  charger_id: number;
  vehicle_model: string;
  soc_start: number;
  soc_target: number;
  hours_until_departure: number;
};

/** POST /api/debug/plug-in — returns once StartTransaction has created the session. */
export type PlugInResponse = {
  session_id: number;
  transaction_id: number | null;
  charger_id: number;
  ocpp_id: string;
};

/**
 * POST /api/llm/extract — what the driver's own sentence stated, or `null` when it stated nothing
 * readable. `target_soc` is a 0..1 fraction and `deadline_iso` carries the site's UTC offset.
 * These two values only ever PREFILL the plug-in form; the driver checks them and submits.
 */
export type ExtractedConstraints = {
  target_soc: number;
  deadline_iso: string;
  confidence: 'high' | 'medium' | 'low';
  detected_language: string;
};

/**
 * POST /api/llm/explain — one session's plan in 2-3 sentences. Narration only: every number the
 * driver reads keeps coming from the computed fields above, never from this text.
 */
export type ExplainText = {
  text: string;
};

/**
 * One car in a demo scenario, as GET /api/demo/status reports it. `status` is "pending" before
 * the scenario reaches it, "plugged_in" once its session exists, "failed" when the plug-in was
 * refused, "skipped" when the run stopped before it.
 */
export type DemoEvent = {
  step: number;
  offset_min: number;
  scheduled_at: string;
  local_time: string;
  charger_id: number;
  vehicle_model: string;
  soc_start: number;
  soc_target: number;
  status: string;
  requested_at: string | null;
  hours_until_departure: number | null;
  session_id: number | null;
  transaction_id: number | null;
  ocpp_id: string | null;
  detail: string | null;
};

/**
 * The fault "fault_injection" injects, as GET /api/demo/status reports it. It is not a step of
 * its own: `status` is "pending" until it fires, then "faulted" (the charge point reported
 * Faulted) or "failed". Null for the three scenarios that inject nothing.
 */
export type DemoFault = {
  offset_min: number;
  scheduled_at: string;
  local_time: string;
  charger_id: number;
  status: string;
  requested_at: string | null;
  ocpp_id: string | null;
  charger_status: string | null;
  detail: string | null;
};

/**
 * GET /api/demo/status — the latest scenario run, running or finished. Every field is null/0/[]
 * when none has been started since the backend came up. `step` counts the plug-ins done.
 */
export type DemoStatus = {
  scenario: string | null;
  running: boolean;
  step: number;
  total_steps: number;
  events: DemoEvent[];
  fault: DemoFault | null;
  error: string | null;
};

/** POST /api/demo/scenario/{name} — the run has been started in the background, not finished. */
export type DemoScenarioStarted = {
  scenario: string;
  started: boolean;
  steps: number;
};

/** One session the reset stopped. `stopped` is false when its StopTransaction never arrived. */
export type DemoResetSession = {
  session_id: number;
  ocpp_id: string;
  remote_stop: string;
  stopped: boolean;
};

/** POST /api/demo/reset — sessions, meter values and schedules are gone; seed data survives. */
export type DemoResetResult = {
  reset: boolean;
  sessions: DemoResetSession[];
  waited_s: number;
  truncated: boolean;
  cancelled_scenario: string | null;
};

/* ------------------------------------------------------------------ errors */

/**
 * A failed request. `status` is the HTTP status, or `NETWORK_ERROR_STATUS` (0) when the
 * request never reached the server (offline, connection refused, 10 s timeout).
 * `body` is the raw response text (or the failure description for a network error).
 */
export class ApiError extends Error {
  readonly status: number;
  readonly body: string;
  readonly url: string;
  /** `detail` lifted out of a FastAPI `{"detail": "..."}` body, when the body is one. */
  readonly detail: string | null;

  constructor(status: number, body: string, url = '') {
    const detail = extractDetail(body);
    const what = status === NETWORK_ERROR_STATUS ? 'network error' : `HTTP ${status}`;
    super(`${what}${url ? ` for ${url}` : ''}${detail ? `: ${detail}` : ''}`);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
    this.url = url;
    this.detail = detail;
  }

  /** True when the request never reached the backend — the dashboard shows its API-down banner. */
  get isNetworkError(): boolean {
    return this.status === NETWORK_ERROR_STATUS;
  }
}

function extractDetail(body: string): string | null {
  const trimmed = body.trim();
  if (!trimmed.startsWith('{')) return trimmed ? trimmed.slice(0, 300) : null;
  try {
    const parsed = JSON.parse(trimmed) as { detail?: unknown };
    if (typeof parsed.detail === 'string') return parsed.detail;
    if (parsed.detail != null) return JSON.stringify(parsed.detail).slice(0, 300);
  } catch {
    /* not JSON — fall through to the raw text */
  }
  return trimmed.slice(0, 300);
}

/** True for the DOMException a caller's own AbortSignal raises (unmount, superseded request). */
export function isAbortError(err: unknown): boolean {
  return err instanceof Error && (err.name === 'AbortError' || err.name === 'CanceledError');
}

/* ------------------------------------------------------------------ transport */

type Linked = { signal: AbortSignal; dispose: () => void };

/**
 * A signal that aborts after `timeoutMs`, or as soon as the caller's signal does.
 * (AbortSignal.any is too new to rely on; this does the same job with one controller.)
 */
function linkSignals(external?: AbortSignal, timeoutMs: number = REQUEST_TIMEOUT_MS): Linked {
  const controller = new AbortController();
  const onExternalAbort = () => controller.abort();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  if (external) {
    if (external.aborted) controller.abort();
    else external.addEventListener('abort', onExternalAbort, { once: true });
  }

  return {
    signal: controller.signal,
    dispose: () => {
      clearTimeout(timer);
      if (external) external.removeEventListener('abort', onExternalAbort);
    },
  };
}

async function request<T>(
  url: string,
  init: RequestInit = {},
  external?: AbortSignal,
  timeoutMs: number = REQUEST_TIMEOUT_MS,
): Promise<T> {
  // The timeout covers reading the body too, so it is only disposed once the text is in hand.
  const link = linkSignals(external, timeoutMs);
  let response: Response;
  let text: string;

  try {
    try {
      response = await fetch(url, {
        ...init,
        signal: link.signal,
        headers: { Accept: 'application/json', ...(init.headers ?? {}) },
      });
      text = await response.text();
    } catch (err) {
      // The caller cancelled (component unmounted): propagate the abort, it is not a failure.
      if (external?.aborted) throw err;
      throw new ApiError(
        NETWORK_ERROR_STATUS,
        link.signal.aborted
          ? `request timed out after ${timeoutMs} ms`
          : err instanceof Error
            ? err.message
            : String(err),
        url,
      );
    }
  } finally {
    link.dispose();
  }

  if (!response.ok) throw new ApiError(response.status, text, url);
  if (!text) return undefined as T;

  try {
    return JSON.parse(text) as T;
  } catch {
    throw new ApiError(response.status, `expected JSON, got: ${text.slice(0, 300)}`, url);
  }
}

function getJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  return request<T>(url, { method: 'GET' }, signal);
}

function postJson<T>(
  url: string,
  body: unknown,
  signal?: AbortSignal,
  timeoutMs?: number,
): Promise<T> {
  return request<T>(
    url,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body ?? {}),
    },
    signal,
    timeoutMs,
  );
}

/* ------------------------------------------------------------------ endpoints */

/** The simulation clock. Drives the "now" line and every axis label — never `Date.now()`. */
export function getClock(signal?: AbortSignal): Promise<ClockInfo> {
  return getJson<ClockInfo>('/api/clock', signal);
}

/** Carbon intensity for the current slot. */
export function getGridLatest(signal?: AbortSignal): Promise<CarbonPoint> {
  return getJson<CarbonPoint>('/api/grid/latest', signal);
}

/** Carbon forecast, `hours * 4` points at 15-minute spacing (24 h = the Gantt's 96 slots). */
export function getGridForecast(hours = 24, signal?: AbortSignal): Promise<CarbonPoint[]> {
  return getJson<CarbonPoint[]>(`/api/grid/forecast?hours=${encodeURIComponent(String(hours))}`, signal);
}

/** Every site with its chargers nested. */
export function getSites(signal?: AbortSignal): Promise<Site[]> {
  return getJson<Site[]>('/api/sites', signal);
}

/** Active sessions, each with its latest 96-slot plan attached. */
export function getActiveSessions(signal?: AbortSignal): Promise<ActiveSession[]> {
  return getJson<ActiveSession[]>('/api/sessions/active', signal);
}

/** The newest persisted plan for one session. Throws ApiError(404) when it has none yet. */
export function getSessionSchedule(sessionId: number, signal?: AbortSignal): Promise<SessionSchedule> {
  return getJson<SessionSchedule>(
    `/api/sessions/${encodeURIComponent(String(sessionId))}/schedule`,
    signal,
  );
}

/** Optimized vs baseline aggregate kW for a site, 96 slots, plus both peaks and the site limit. */
export function getLoadCurve(siteId: number, signal?: AbortSignal): Promise<LoadCurve> {
  return getJson<LoadCurve>(
    `/api/sites/${encodeURIComponent(String(siteId))}/load-curve`,
    signal,
  );
}

/** CO2 and rupees saved against the baseline, plus the on-time session count. */
export function getImpactSummary(signal?: AbortSignal): Promise<ImpactSummary> {
  return getJson<ImpactSummary>('/api/impact/summary', signal);
}

/** Raw OCPP frames, newest first (the backend keeps the most recent ones only). */
export function getOcppLog(limit = 50, signal?: AbortSignal): Promise<OcppFrame[]> {
  return getJson<OcppFrame[]>(`/api/ocpp/log?limit=${encodeURIComponent(String(limit))}`, signal);
}

/** Set the optimizer weights. The backend re-ticks immediately and returns that tick. */
export function postWeights(alpha: number, beta: number, signal?: AbortSignal): Promise<WeightsResponse> {
  return postJson<WeightsResponse>('/api/optimizer/weights', { alpha, beta }, signal);
}

/** Operator override: charge this session at full power now. */
export function postOverride(sessionId: number, signal?: AbortSignal): Promise<OverrideResponse> {
  return postJson<OverrideResponse>(
    `/api/sessions/${encodeURIComponent(String(sessionId))}/override`,
    {},
    signal,
  );
}

/** The vehicle catalogue for the driver's plug-in form. */
export function getVehicles(signal?: AbortSignal): Promise<Vehicle[]> {
  return getJson<Vehicle[]>('/api/vehicles', signal);
}

/** One session's savings, ETA and green/grey split. Throws ApiError(404) for an unknown session. */
export function getSessionImpact(sessionId: number, signal?: AbortSignal): Promise<SessionImpact> {
  return getJson<SessionImpact>(
    `/api/sessions/${encodeURIComponent(String(sessionId))}/impact`,
    signal,
  );
}

/** The cost of overriding, to show before the driver confirms. Throws ApiError(404) when unknown. */
export function getOverridePreview(
  sessionId: number,
  signal?: AbortSignal,
): Promise<OverridePreview> {
  return getJson<OverridePreview>(
    `/api/sessions/${encodeURIComponent(String(sessionId))}/override-preview`,
    signal,
  );
}

/** Plug a car in. Resolves once the charge point's StartTransaction has created the session. */
export function postPlugIn(body: PlugInRequest, signal?: AbortSignal): Promise<PlugInResponse> {
  return postJson<PlugInResponse>('/api/debug/plug-in', body, signal);
}

/**
 * The driver's own sentence (English, Hindi or Gujarati) read into the two plug-in values, or
 * `null` when it stated neither. Extraction assists the form and never submits it, so every
 * failure here — `null`, 503 with no key configured, or the timeout above — is answered by
 * leaving the form exactly as the driver left it.
 */
export function postLlmExtract(
  text: string,
  signal?: AbortSignal,
): Promise<ExtractedConstraints | null> {
  return postJson<ExtractedConstraints | null>(
    '/api/llm/extract',
    { text },
    signal,
    LLM_REQUEST_TIMEOUT_MS,
  );
}

/**
 * One session's plan narrated in plain language. Always 200 with text when the LLM layer is
 * configured (the backend falls back to deterministic sentences of its own); throws
 * ApiError(404) for an unknown session and ApiError(503) when no key is configured.
 */
export function postLlmExplain(sessionId: number, signal?: AbortSignal): Promise<ExplainText> {
  return postJson<ExplainText>(
    '/api/llm/explain',
    { session_id: sessionId },
    signal,
    LLM_REQUEST_TIMEOUT_MS,
  );
}

/**
 * The latest demo run, running or finished. A cheap in-memory read on the backend: it touches
 * neither the database nor the charge points, so the demo panel may poll it while a run is live.
 */
export function getDemoStatus(signal?: AbortSignal): Promise<DemoStatus> {
  return getJson<DemoStatus>('/api/demo/status', signal);
}

/**
 * Start a demo scenario. Resolves as soon as the run has been accepted — the cars plug in over
 * the following simulated minutes, so the caller watches {@link getDemoStatus} for progress.
 * Throws ApiError(404) for an unknown name and ApiError(409) when a scenario is already running
 * or a reset is in progress; neither is a failure worth alarming the operator with.
 */
export function postDemoScenario(name: string, signal?: AbortSignal): Promise<DemoScenarioStarted> {
  return postJson<DemoScenarioStarted>(
    `/api/demo/scenario/${encodeURIComponent(name)}`,
    {},
    signal,
  );
}

/**
 * Clear all demo state: cancel a running scenario, RemoteStop every active session, then truncate
 * sessions, schedules and meter values. Seed data and the carbon cache survive. Throws
 * ApiError(503) when the database refused the work.
 */
export function postDemoReset(signal?: AbortSignal): Promise<DemoResetResult> {
  return postJson<DemoResetResult>('/api/demo/reset', {}, signal, DEMO_RESET_TIMEOUT_MS);
}

/** Alias of {@link postWeights}. */
export const setWeights = postWeights;
/** Alias of {@link postOverride}. */
export const overrideSession = postOverride;

/** Every endpoint in one object, for callers that prefer `api.getSites()`. */
export const api = {
  getClock,
  getGridLatest,
  getGridForecast,
  getSites,
  getActiveSessions,
  getSessionSchedule,
  getLoadCurve,
  getImpactSummary,
  getOcppLog,
  postWeights,
  postOverride,
  getVehicles,
  getSessionImpact,
  getOverridePreview,
  postPlugIn,
  postLlmExtract,
  postLlmExplain,
  getDemoStatus,
  postDemoScenario,
  postDemoReset,
};

export default api;
