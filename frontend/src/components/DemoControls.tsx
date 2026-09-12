/*
 * DemoControls — the one-click scenario buttons (BUILD_SPEC Phase 8).
 *
 * "Never improvise state during a live demo": every scenario is a deterministic script the
 * backend owns, so this panel only ever names one and watches it. It computes nothing — the
 * steps, their times and their outcomes all come from GET /api/demo/status, and the four
 * descriptions below are the ones in backend/app/scenarios.py, word for word.
 *
 * Behaviour that matters on stage:
 *  - a scenario cannot be started while one is running (the buttons lock, and the backend's own
 *    409 is shown as one calm sentence if the click still lands);
 *  - a 404 or a 503 is one quiet sentence too — nothing on this panel is ever a red box or a
 *    stack trace, because the operator can do nothing about either mid-demo;
 *  - Reset stays available WHILE a scenario runs: it cancels the run and clears the state, and it
 *    is the only way out of a scenario that has gone wrong. It takes two clicks (the second one
 *    arms for six seconds, then disarms itself) so an elbow cannot destroy the demo;
 *  - the status endpoint is polled at the spec's 3 s only while something is actually happening.
 *    When nothing is, the poll goes quiet: `watchRef` short-circuits the fetcher, so the timer
 *    keeps ticking (at the slower idle interval) without a request ever leaving the browser. The
 *    one exception is the fetch on mount, which adopts a run that was already in progress.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';

import { ApiError, getDemoStatus, postDemoReset, postDemoScenario } from '../api/client';
import type { DemoEvent, DemoStatus } from '../api/client';
import { useLiveData } from '../hooks/useLiveData';

/* ------------------------------------------------------------------ constants */

const POLL_MS = 3000; // BUILD_SPEC Phase 5: never poll more often than every 3 seconds.
const IDLE_POLL_MS = 15000; // nothing is running: the timer turns over, no request is sent.
const CONFIRM_MS = 6000; // how long the armed Reset button stays armed.

/**
 * The four scenarios, in demo order. `name` is the path segment POST /api/demo/scenario/{name}
 * takes and `description` is the scenario's own description from backend/app/scenarios.py — not
 * a paraphrase. `title` is the same name spelled for a human reading it from four metres away.
 */
const SCENARIOS: ReadonlyArray<{
  name: string;
  title: string;
  description: string;
  headline?: boolean;
}> = [
  {
    name: 'evening_rush',
    title: 'Evening rush',
    description: '6 cars plug in 18:30-19:15, all leave 07:00. The headline scenario.',
    headline: true,
  },
  {
    name: 'workplace_solar',
    title: 'Workplace solar',
    description: '6 cars plug in 09:00-09:30, leave 18:00. Shows the midday solar capture.',
  },
  {
    name: 'tight_deadline',
    title: 'Tight deadline',
    description: '1 car needs 45 kWh in 3 hours. Demonstrates the relaxed/infeasible path.',
  },
  {
    name: 'fault_injection',
    title: 'Fault injection',
    description: 'Mid-session, CP003 goes Faulted. Shows rebalancing.',
  },
];

/** Event statuses GET /api/demo/status uses. */
const PLUGGED_IN = 'plugged_in';
const FAILED = 'failed';
const FAULTED = 'faulted';

/* ------------------------------------------------------------------ helpers */

function titleOf(name: string | null): string {
  if (!name) return '—';
  const known = SCENARIOS.find((scenario) => scenario.name === name);
  return known ? known.title : name;
}

/**
 * What to put in front of the operator when a demo request fails: one sentence they can act on.
 * A 409 is the expected answer to an impatient second click, not an error; a 404 or a 503 is a
 * backend fact nothing on this screen can fix. None of them earns a red box mid-demo.
 */
function noteForError(err: unknown, what: string): Note {
  if (err instanceof ApiError) {
    if (err.status === 409) {
      return {
        tone: 'warn',
        text: 'A scenario is already running — let it finish, or reset first.',
        transient: true,
      };
    }
    if (err.status === 404) {
      return { tone: 'quiet', text: `This backend does not know the scenario ${what}.` };
    }
    if (err.status === 503) {
      return { tone: 'warn', text: 'The reset could not finish — the database did not answer. Try it again.' };
    }
    if (err.isNetworkError) {
      return { tone: 'warn', text: 'The backend did not answer in time — check that it is still running.' };
    }
    return { tone: 'quiet', text: err.detail ?? err.message };
  }
  return { tone: 'quiet', text: err instanceof Error ? err.message : String(err) };
}

/** `transient` marks a note that only made sense while a scenario was running (the 409). */
type Note = { tone: 'quiet' | 'warn'; text: string; transient?: boolean };

type Action = { kind: 'idle' } | { kind: 'starting'; name: string } | { kind: 'resetting' };

/* ------------------------------------------------------------------ pieces */

const CHIP_BASE =
  'inline-flex items-center gap-1.5 whitespace-nowrap rounded-md border px-1.5 py-0.5 text-[11px] tabular-nums';

const CHIP_TONE: Record<string, string> = {
  [PLUGGED_IN]: 'border-emerald-300 bg-emerald-50 text-emerald-800',
  [FAILED]: 'border-red-300 bg-red-50 text-red-800',
  [FAULTED]: 'border-amber-400 bg-amber-50 text-amber-900',
  skipped: 'border-slate-200 bg-white text-slate-400',
  pending: 'border-slate-200 bg-slate-50 text-slate-500',
};

const DOT_TONE: Record<string, string> = {
  [PLUGGED_IN]: 'bg-emerald-500',
  [FAILED]: 'bg-red-500',
  [FAULTED]: 'bg-amber-500',
  skipped: 'bg-slate-300',
  pending: 'bg-slate-300',
};

function Chip({ status, children }: { status: string; children: ReactNode }) {
  return (
    <span className={CHIP_BASE + ' ' + (CHIP_TONE[status] ?? CHIP_TONE.pending)}>
      <span className={'h-1.5 w-1.5 shrink-0 rounded-full ' + (DOT_TONE[status] ?? DOT_TONE.pending)} />
      {children}
    </span>
  );
}

function EventChip({ event }: { event: DemoEvent }) {
  return (
    <Chip status={event.status}>
      <span className="font-mono">{event.local_time}</span>
      <span className="font-semibold">{event.ocpp_id ?? 'CP' + String(event.charger_id).padStart(3, '0')}</span>
      <span className="max-w-[120px] truncate font-normal">{event.vehicle_model}</span>
    </Chip>
  );
}

/* ------------------------------------------------------------------ the panel */

export interface DemoControlsProps {
  /** True when the site has no active sessions, so the panel can say why the plan is empty. */
  idle?: boolean;
  /** Called after a scenario starts, after it finishes and after a reset, so the page refetches. */
  onChanged?: () => void;
  /** Lock the controls (e.g. while the API is unreachable). */
  disabled?: boolean;
}

export default function DemoControls({
  idle = false,
  onChanged,
  disabled = false,
}: DemoControlsProps) {
  const [action, setAction] = useState<Action>({ kind: 'idle' });
  const [note, setNote] = useState<Note | null>(null);
  const [armed, setArmed] = useState(false);
  // A reset leaves the finished run in the backend's memory; the panel still returns to idle.
  const [hideRun, setHideRun] = useState(false);

  const mounted = useRef(true);
  const watchRef = useRef(true); // the fetch on mount always goes out; see the file header.
  const lastRef = useRef<DemoStatus | null>(null);
  const onChangedRef = useRef(onChanged);
  const wasRunning = useRef(false);

  useEffect(() => {
    onChangedRef.current = onChanged;
  });

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  // `activeRun` is state, not a ref, so the hook re-arms its timer the render after a run starts
  // or ends: 3 s while something is happening, the idle interval (no request at all) otherwise.
  const [activeRun, setActiveRun] = useState(false);

  const statusFeed = useLiveData<DemoStatus | null>(
    (signal) => (watchRef.current ? getDemoStatus(signal) : Promise.resolve(lastRef.current)),
    action.kind !== 'idle' || activeRun ? POLL_MS : IDLE_POLL_MS,
  );

  const status = statusFeed.data;
  const running = status?.running ?? false;
  const busy = action.kind !== 'idle';
  const watching = busy || running;

  useEffect(() => {
    if (status) lastRef.current = status;
    watchRef.current = watching;
    setActiveRun(running);
  }, [status, watching, running]);

  // A run that has just ended changed the sessions, the plan and the OCPP log: pull them now
  // rather than leaving the page to notice on its own schedule.
  useEffect(() => {
    if (running) wasRunning.current = true;
    else if (wasRunning.current) {
      wasRunning.current = false;
      // "a scenario is already running" stops being true the moment it stops running.
      setNote((prev) => (prev?.transient ? null : prev));
      onChangedRef.current?.();
    }
  }, [running]);

  // The armed Reset disarms itself, so a half-pressed button cannot sit red through a demo.
  useEffect(() => {
    if (!armed) return undefined;
    const timer = window.setTimeout(() => setArmed(false), CONFIRM_MS);
    return () => window.clearTimeout(timer);
  }, [armed]);

  const refresh = statusFeed.refresh;

  const start = useCallback(
    async (name: string) => {
      setArmed(false);
      setNote(null);
      setHideRun(false);
      setAction({ kind: 'starting', name });
      watchRef.current = true;
      try {
        await postDemoScenario(name);
        if (mounted.current) {
          wasRunning.current = true; // so the end of the run always refreshes the page
          setNote(null);
        }
        onChangedRef.current?.();
      } catch (err) {
        if (mounted.current) setNote(noteForError(err, name));
      } finally {
        if (mounted.current) setAction({ kind: 'idle' });
        refresh();
      }
    },
    [refresh],
  );

  const reset = useCallback(async () => {
    setArmed(false);
    setNote(null);
    setAction({ kind: 'resetting' });
    watchRef.current = true;
    try {
      const result = await postDemoReset();
      if (mounted.current) {
        const stopped = result.sessions.length;
        setHideRun(true);
        wasRunning.current = false;
        setNote({
          tone: 'quiet',
          text:
            stopped === 0
              ? 'Demo state cleared. Seed chargers and grid data are untouched.'
              : `Demo state cleared — ${stopped} session${stopped === 1 ? '' : 's'} stopped. Seed chargers and grid data are untouched.`,
        });
      }
      onChangedRef.current?.();
    } catch (err) {
      if (mounted.current) setNote(noteForError(err, 'reset'));
    } finally {
      if (mounted.current) setAction({ kind: 'idle' });
      refresh();
    }
  }, [refresh]);

  const events = status?.events ?? [];
  const fault = status?.fault ?? null;
  const total = status?.total_steps ?? 0;
  const step = status?.step ?? 0;
  // A finished run whose sessions are gone (a reset, or a page opened long after) is history,
  // not state: the panel goes back to idle rather than showing steps nothing on the page matches.
  // A run that failed keeps its summary either way — that is the one thing worth reading twice.
  const ranAway = !running && idle && !status?.error;
  const showRun =
    !hideRun && !ranAway && status != null && status.scenario != null && events.length > 0;
  const starting = action.kind === 'starting' ? action.name : null;
  const activeName = running ? status?.scenario ?? null : starting;
  const locked = disabled || busy || running;

  const headline = showRun
    ? running
      ? `${titleOf(status?.scenario ?? null)} · step ${step} of ${total} · running`
      : status?.error
        ? `${titleOf(status?.scenario ?? null)} · stopped at step ${step} of ${total}`
        : `${titleOf(status?.scenario ?? null)} · ${step} of ${total} cars plugged in · done`
    : null;

  return (
    <section className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
      <header className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <h2 className="text-sm font-semibold text-slate-900">Demo scenarios</h2>
        <p className="text-[11px] text-slate-500">
          {action.kind === 'resetting'
            ? 'clearing demo state…'
            : starting
              ? `starting ${titleOf(starting)}…`
              : running
                ? 'a scenario is running — the buttons unlock when it finishes'
                : 'each one resets the state first, then seeds it deterministically'}
        </p>
      </header>

      <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-4">
        {SCENARIOS.map((scenario) => {
          const isActive = activeName === scenario.name;
          const tone = scenario.headline
            ? 'border-emerald-700 bg-emerald-600 text-white hover:bg-emerald-700'
            : 'border-slate-300 bg-white text-slate-900 hover:border-slate-400 hover:bg-slate-50';
          return (
            <button
              key={scenario.name}
              type="button"
              onClick={() => {
                void start(scenario.name);
              }}
              disabled={locked}
              title={scenario.description}
              className={
                'flex h-full min-w-0 flex-col rounded-lg border px-3 py-2 text-left disabled:cursor-not-allowed disabled:opacity-50 ' +
                tone +
                (isActive ? ' ring-2 ring-emerald-400 ring-offset-1' : '')
              }
            >
              <span className="flex w-full items-baseline justify-between gap-2">
                <span className="truncate text-[13px] font-semibold">{scenario.title}</span>
                {isActive ? (
                  <span
                    className={
                      'shrink-0 rounded px-1 text-[9px] font-bold uppercase tracking-wide ' +
                      (scenario.headline ? 'bg-white/25 text-white' : 'bg-emerald-100 text-emerald-800')
                    }
                  >
                    running
                  </span>
                ) : scenario.headline ? (
                  <span className="shrink-0 rounded bg-white/20 px-1 text-[9px] font-bold uppercase tracking-wide">
                    headline
                  </span>
                ) : null}
              </span>
              <span
                className={
                  'mt-0.5 block truncate font-mono text-[10px] ' +
                  (scenario.headline ? 'text-emerald-100' : 'text-slate-400')
                }
              >
                {scenario.name}
              </span>
              <span
                className={
                  'mt-1 block text-[11px] leading-4 ' +
                  (scenario.headline ? 'text-emerald-50' : 'text-slate-500')
                }
              >
                {scenario.description}
              </span>
            </button>
          );
        })}
      </div>

      {showRun ? (
        <div className="mt-3">
          <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
            <p className="text-[11px] font-semibold text-slate-700 tabular-nums">{headline}</p>
            <p className="font-mono text-[10px] text-slate-400">{status?.scenario}</p>
          </div>
          <div className="mt-1 h-1 w-full overflow-hidden rounded-full bg-slate-200">
            <div
              className={'h-full rounded-full ' + (status?.error ? 'bg-amber-500' : 'bg-emerald-500')}
              style={{ width: total > 0 ? `${Math.min(100, (step / total) * 100)}%` : '0%' }}
            />
          </div>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {events.map((event) => (
              <EventChip key={event.step} event={event} />
            ))}
            {fault ? (
              <Chip status={fault.status}>
                <span className="font-mono">{fault.local_time}</span>
                <span className="font-semibold">
                  {fault.ocpp_id ?? 'CP' + String(fault.charger_id).padStart(3, '0')}
                </span>
                <span className="font-normal">
                  {fault.status === FAULTED
                    ? 'faulted — rebalancing'
                    : fault.status === FAILED
                      ? 'fault not delivered'
                      : 'fault due'}
                </span>
              </Chip>
            ) : null}
          </div>
          {status?.error ? (
            <p className="mt-2 rounded border border-amber-200 bg-amber-50 px-2 py-1 text-[11px] leading-4 text-amber-800">
              The run stopped early — {status.error}
            </p>
          ) : null}
        </div>
      ) : null}

      {note ? (
        <p
          className={
            'mt-3 rounded border px-2 py-1 text-[11px] leading-4 ' +
            (note.tone === 'warn'
              ? 'border-amber-200 bg-amber-50 text-amber-800'
              : 'border-slate-200 bg-slate-50 text-slate-600')
          }
        >
          {note.text}
        </p>
      ) : null}

      {statusFeed.error && watching ? (
        <p className="mt-3 rounded border border-slate-200 bg-slate-50 px-2 py-1 text-[11px] leading-4 text-slate-600">
          Progress updates paused — the demo status endpoint did not answer. The scenario itself is
          unaffected.
        </p>
      ) : null}

      {!showRun && idle && !busy ? (
        <p className="mt-3 text-[11px] leading-4 text-slate-500">
          No cars are charging, so the plan below has no rows yet — the carbon background stays
          live either way. Press a scenario to seed one.
        </p>
      ) : null}

      <div className="mt-3 flex flex-wrap items-center justify-between gap-x-4 gap-y-2 border-t border-slate-200 pt-3">
        <p className="min-w-0 max-w-2xl text-[11px] leading-4 text-slate-500">
          <span className="font-semibold text-slate-600">Reset</span> stops every session and
          clears sessions, schedules and meter values. Sites, chargers and the carbon cache
          survive. It also cancels a scenario that is still running.
        </p>
        {armed ? (
          <span className="flex shrink-0 items-center gap-2">
            <span className="text-[11px] font-semibold text-red-700">Destroy all session state?</span>
            <button
              type="button"
              onClick={() => {
                void reset();
              }}
              disabled={disabled || busy}
              className="rounded-lg border border-red-700 bg-red-600 px-3 py-1.5 text-[13px] font-semibold text-white hover:bg-red-700 disabled:cursor-not-allowed disabled:opacity-50"
            >
              Yes, reset
            </button>
            <button
              type="button"
              onClick={() => setArmed(false)}
              className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-[13px] font-semibold text-slate-700 hover:bg-slate-50"
            >
              Cancel
            </button>
          </span>
        ) : (
          <button
            type="button"
            onClick={() => setArmed(true)}
            disabled={disabled || busy}
            title="Two clicks: this destroys every session, schedule and meter value."
            className="shrink-0 rounded-lg border border-red-300 bg-white px-3 py-1.5 text-[13px] font-semibold text-red-700 hover:bg-red-50 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {action.kind === 'resetting' ? 'Resetting…' : 'Reset demo state'}
          </button>
        )}
      </div>
    </section>
  );
}
