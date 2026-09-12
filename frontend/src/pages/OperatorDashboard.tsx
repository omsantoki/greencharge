/*
 * OperatorDashboard — the screen the demo runs on (BUILD_SPEC Phase 5).
 *
 * Every value on this page comes from the backend through useLiveData (3 s poll, the fastest the
 * spec allows). Nothing is computed here, and the browser clock is never used: the header clock is
 * the SIMULATED clock from GET /api/clock and every timestamp is rendered in Asia/Kolkata.
 *
 * Layout, pinned by the implementation contract:
 *   header -> ImpactScorecard + CarbonGauge + WeightSlider -> ScheduleGantt (full width)
 *          -> LoadCurve + ChargerGrid -> OcppLog
 *
 * Every panel sits in a fixed-height slot so arriving data never moves the page, and every panel
 * is wrapped in an error boundary so one failing panel cannot blank the dashboard mid-demo.
 * There are no animations anywhere.
 */

import { Component, useEffect } from 'react';
import type { ReactNode } from 'react';

import {
  getActiveSessions,
  getClock,
  getGridForecast,
  getGridLatest,
  getImpactSummary,
  getLoadCurve,
  getOcppLog,
  getSites,
  postWeights,
} from '../api/client';
import type { CarbonPoint, Site } from '../api/client';
import { useLiveData } from '../hooks/useLiveData';

import CarbonGauge from '../components/CarbonGauge';
import ChargerGrid from '../components/ChargerGrid';
import DemoControls from '../components/DemoControls';
import ImpactScorecard from '../components/ImpactScorecard';
import LoadCurve from '../components/LoadCurve';
import OcppLog from '../components/OcppLog';
import ScheduleGantt from '../components/ScheduleGantt';
import WeightSlider from '../components/WeightSlider';

/* ------------------------------------------------------------------ constants */

const POLL_MS = 3000; // BUILD_SPEC Phase 5: never poll more often than every 3 seconds.
const SLOW_POLL_MS = 15000; // the 24 h carbon forecast only advances one 15-minute slot at a time.
const FORECAST_HOURS = 24;
const OCPP_LOG_LIMIT = 50;
const SITE_TZ = 'Asia/Kolkata';

/* ------------------------------------------------------------------ formatting */

const clockTimeFormat = new Intl.DateTimeFormat('en-GB', {
  timeZone: SITE_TZ,
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
});

const clockDateFormat = new Intl.DateTimeFormat('en-GB', {
  timeZone: SITE_TZ,
  weekday: 'short',
  day: '2-digit',
  month: 'short',
});

function parseIso(value: string | null): Date | null {
  if (!value) return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function formatClockTime(iso: string | null): string {
  const parsed = parseIso(iso);
  return parsed ? clockTimeFormat.format(parsed) : '--:--:--';
}

function formatClockDate(iso: string | null): string {
  const parsed = parseIso(iso);
  return parsed ? clockDateFormat.format(parsed) : 'waiting for /api/clock';
}

/**
 * The Gantt's SVG keeps a fixed aspect ratio and grows a row per session, so its rendered height
 * depends on the row count. These are the measured heights (at the page's widest, 1680px) that hold
 * the whole chart without clipping; anything taller would just be white space inside the card.
 */
function ganttSlotHeight(sessionCount: number): string {
  if (sessionCount === 0) return 'h-[280px]';
  if (sessionCount <= 2) return 'h-[304px]';
  if (sessionCount <= 4) return 'h-[392px]';
  return 'h-[452px]';
}

function formatKw(value: number | null | undefined): string {
  return typeof value === 'number' && Number.isFinite(value) ? String(Math.round(value)) : '?';
}

/* ------------------------------------------------------------------ shell bits */

function PanelMessage({ title, body }: { title: string; body?: string }) {
  return (
    <div className="flex h-full w-full flex-col items-center justify-center gap-1 rounded-xl border border-slate-200 bg-white p-4 text-center shadow-sm">
      <div className="text-sm font-semibold text-slate-600">{title}</div>
      {body ? <div className="max-w-md text-xs text-slate-500">{body}</div> : null}
    </div>
  );
}

/** Keeps a broken panel from taking the whole dashboard down during the demo. */
class PanelBoundary extends Component<{ name: string; children: ReactNode }, { failed: boolean }> {
  constructor(props: { name: string; children: ReactNode }) {
    super(props);
    this.state = { failed: false };
  }

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch(error: unknown) {
    console.error('[OperatorDashboard] panel "' + this.props.name + '" crashed', error);
  }

  render() {
    if (this.state.failed) {
      return (
        <PanelMessage
          title={this.props.name + ' stopped rendering'}
          body="The rest of the dashboard is unaffected — see the browser console for details."
        />
      );
    }
    return this.props.children;
  }
}

/**
 * One fixed-height slot in the grid. `[&>*]:h-full` stretches the panel inside it, so panels in a
 * row line up and a refresh can never change the page height.
 */
function Slot({
  name,
  className,
  children,
}: {
  name: string;
  className: string;
  children: ReactNode;
}) {
  return (
    <div className={'overflow-hidden [&>*]:h-full ' + className}>
      <PanelBoundary name={name}>{children}</PanelBoundary>
    </div>
  );
}

function SourceBadge({ source }: { source: string | null }) {
  const base =
    'whitespace-nowrap rounded-md border px-2 py-1 text-[11px] font-semibold uppercase tracking-wide';
  if (!source) {
    return (
      <span className={base + ' border-slate-300 bg-slate-100 text-slate-500'}>
        grid data: waiting
      </span>
    );
  }
  if (source === 'estimated') {
    // Honesty rule: synthetic carbon values are never presented as measured.
    return (
      <span
        className={base + ' border-amber-400 bg-amber-100 text-amber-900'}
        title="Carbon values come from the synthetic provider — estimates, not measurements."
      >
        grid data: estimated
      </span>
    );
  }
  return (
    <span
      className={base + ' border-sky-400 bg-sky-100 text-sky-900'}
      title={'Grid data source: ' + source}
    >
      {'grid data: ' + source}
    </span>
  );
}

function HealthDot({ ok }: { ok: boolean }) {
  return (
    <span className="flex w-14 items-center justify-end gap-1.5 whitespace-nowrap text-[11px] font-semibold uppercase tracking-wide">
      <span className={'h-2.5 w-2.5 rounded-full ' + (ok ? 'bg-emerald-500' : 'bg-red-500')} />
      <span className={ok ? 'text-emerald-700' : 'text-red-700'}>{ok ? 'live' : 'stale'}</span>
    </span>
  );
}

/* ------------------------------------------------------------------- the page */

export default function OperatorDashboard() {
  const clockFeed = useLiveData(getClock, POLL_MS);
  const carbonFeed = useLiveData(getGridLatest, POLL_MS);
  const forecastFeed = useLiveData(
    (signal) => getGridForecast(FORECAST_HOURS, signal),
    SLOW_POLL_MS,
  );
  const sitesFeed = useLiveData(getSites, POLL_MS);
  const sessionsFeed = useLiveData(getActiveSessions, POLL_MS);
  const impactFeed = useLiveData(getImpactSummary, POLL_MS);
  const ocppFeed = useLiveData((signal) => getOcppLog(OCPP_LOG_LIMIT, signal), POLL_MS);

  const site: Site | null = sitesFeed.data && sitesFeed.data.length > 0 ? sitesFeed.data[0] : null;
  const siteId = site ? site.id : null;

  // The load curve is per-site, so its fetcher closes over the id the /api/sites poll produced.
  const loadCurveFeed = useLiveData(
    (signal) => (siteId == null ? Promise.resolve(null) : getLoadCurve(siteId, signal)),
    POLL_MS,
  );

  // useLiveData reads its fetcher through a ref, so a changed id would otherwise wait one poll.
  const refreshLoadCurve = loadCurveFeed.refresh;
  useEffect(() => {
    if (siteId != null) refreshLoadCurve();
  }, [siteId, refreshLoadCurve]);

  // `live` is false for a feed that is not asking the backend for anything yet: the load curve
  // needs a site id, so while /api/sites has not answered it can neither succeed nor fail. It
  // must stay out of the "is everything down?" count, or a total outage — where the site id
  // never arrives — would forever look like a partial one.
  const feeds: Array<[string, { error: Error | null; data: unknown; refresh: () => void }, boolean]> =
    [
      ['clock', clockFeed, true],
      ['carbon intensity', carbonFeed, true],
      ['carbon forecast', forecastFeed, true],
      ['site', sitesFeed, true],
      ['sessions', sessionsFeed, true],
      ['load curve', loadCurveFeed, siteId != null],
      ['impact', impactFeed, true],
      ['OCPP log', ocppFeed, true],
    ];
  const liveFeeds = feeds.filter(([, , live]) => live);
  const failing = liveFeeds.filter(([, feed]) => feed.error !== null).map(([label]) => label);
  const everythingDown = failing.length > 0 && failing.length === liveFeeds.length;
  const haveSomeData = feeds.some(([, feed]) => feed.data != null);

  // WeightSlider POSTs the weights itself and the backend re-ticks before replying, so pull the
  // four feeds a re-plan changes the moment it reports back: that is what reshapes the Gantt
  // "within 5 seconds". The grid, clock and site feeds are untouched by a re-plan, so they keep
  // their own cadence rather than adding requests.
  function refreshAfterReplan() {
    sessionsFeed.refresh();
    loadCurveFeed.refresh();
    impactFeed.refresh();
    ocppFeed.refresh();
  }

  const clock = clockFeed.data;
  const nowIso = clock ? clock.now : null;
  const timeScale = clock ? clock.time_scale : null;

  const sessions = sessionsFeed.data ?? [];
  const forecast: CarbonPoint[] = forecastFeed.data ?? [];
  const frames = ocppFeed.data ?? [];
  const chargerCount = site ? site.chargers.length : 0;

  const source =
    (forecast.length > 0 ? forecast[0].source : null) ??
    (carbonFeed.data ? carbonFeed.data.source : null);

  // Idle means the sessions request succeeded and came back empty — not "no answer yet".
  const idle = sessionsFeed.error === null && sessionsFeed.data !== null && sessions.length === 0;

  return (
    <div className="min-h-screen bg-slate-100 text-slate-900">
      <header className="sticky top-0 z-20 border-b border-slate-300 bg-white">
        <div className="mx-auto flex max-w-[1680px] flex-wrap items-center gap-x-6 gap-y-2 px-5 py-2.5">
          <div className="flex items-baseline gap-3">
            <span className="text-xl font-bold tracking-tight text-emerald-700">GreenCharge</span>
            <span className="text-sm font-semibold text-slate-500">Operator Dashboard</span>
          </div>
          <div className="min-w-0 truncate text-xs text-slate-500">
            {site
              ? site.name +
                ' · zone ' +
                site.grid_zone +
                ' · site limit ' +
                formatKw(site.max_power_kw) +
                ' kW · ' +
                chargerCount +
                ' chargers'
              : 'site not loaded'}
          </div>
          <div className="ml-auto flex items-center gap-4">
            <SourceBadge source={source} />
            <div className="text-right leading-tight">
              <div className="font-mono text-2xl font-bold tabular-nums text-slate-900">
                {formatClockTime(nowIso)}
              </div>
              <div className="text-[11px] text-slate-500">
                {formatClockDate(nowIso)} · simulated time (IST)
                {timeScale ? ' · ' + timeScale + '× real' : ''}
              </div>
            </div>
            <HealthDot ok={failing.length === 0} />
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-[1680px] space-y-4 px-5 py-4">
        {failing.length > 0 ? (
          <div className="rounded-xl border border-red-400 bg-red-50 px-4 py-3 text-sm text-red-900">
            <div className="font-bold">
              {everythingDown
                ? 'API unreachable — the GreenCharge backend is not responding.'
                : 'Some API requests are failing: ' + failing.join(', ') + '.'}
            </div>
            <div className="mt-1 text-red-800">
              {haveSomeData
                ? 'The panels below are showing the last values that arrived.'
                : 'No data has arrived yet.'}{' '}
              The dashboard reaches the backend through the dev server&apos;s{' '}
              <span className="font-mono">/api</span> proxy — check that the API is running and
              that the proxy target in <span className="font-mono">frontend/vite.config.ts</span>{' '}
              (<span className="font-mono">VITE_API_TARGET</span>) points at it.
            </div>
          </div>
        ) : null}

        <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
          <Slot name="ImpactScorecard" className="h-[212px] lg:col-span-5">
            <ImpactScorecard data={impactFeed.data} />
          </Slot>
          <Slot name="CarbonGauge" className="h-[212px] lg:col-span-3">
            <CarbonGauge data={carbonFeed.data} forecast={forecast} />
          </Slot>
          <Slot name="WeightSlider" className="h-[212px] lg:col-span-4">
            <WeightSlider
              onApplied={refreshAfterReplan}
              postWeights={postWeights}
              disabled={everythingDown}
            />
          </Slot>
        </div>

        {/*
          Phase 8: the scenarios are one click each. The panel is always present — the judge never
          touches a terminal — and it owns its own poll of GET /api/demo/status.
        */}
        <PanelBoundary name="DemoControls">
          <DemoControls idle={idle} onChanged={refreshAfterReplan} disabled={everythingDown} />
        </PanelBoundary>

        {/*
          The plan is the hero. Its SVG is a fixed aspect ratio, so its height follows the number of
          rows; the slot steps to match instead of leaving a tall card mostly white. The height only
          changes when a car plugs in or leaves — a real event, never a refresh.
        */}
        <Slot name="ScheduleGantt" className={ganttSlotHeight(sessions.length)}>
          <section className="flex flex-col overflow-hidden rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
            <h2 className="mb-2 text-sm font-semibold text-slate-900">
              Charging plan — next 24 hours
            </h2>
            <ScheduleGantt sessions={sessions} forecast={forecast} now={nowIso ?? ''} />
          </section>
        </Slot>

        <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
          <Slot name="LoadCurve" className="h-[392px] lg:col-span-7">
            <LoadCurve data={loadCurveFeed.data} />
          </Slot>
          <Slot name="ChargerGrid" className="h-[392px] lg:col-span-5">
            <ChargerGrid
              site={site}
              sessions={sessionsFeed.data}
              now={nowIso}
              timeScale={timeScale}
            />
          </Slot>
        </div>

        {/*
          OcppLog paints its own dark surface edge to edge and sizes its scroll area from the
          slot, so the slot supplies the rounded corner every other panel has.
        */}
        <Slot name="OcppLog" className="h-[284px] rounded-xl">
          <OcppLog frames={frames} limit={OCPP_LOG_LIMIT} />
        </Slot>

        <footer className="pb-2 text-[11px] text-slate-500">
          Live panels refresh every {POLL_MS / 1000} s and the carbon forecast every{' '}
          {SLOW_POLL_MS / 1000} s. All times are simulated site-local time (Asia/Kolkata); the
          simulated clock runs at the backend&apos;s TIME_SCALE.
        </footer>
      </main>
    </div>
  );
}
