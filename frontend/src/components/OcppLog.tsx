import { useMemo } from 'react';

/**
 * OcppLog — the raw OCPP 1.6-J frames exchanged between the charge points and
 * the CSMS (GET /api/ocpp/log?limit=50), newest first, capped at 50.
 *
 * Frames are printed verbatim and wrapped, never truncated: the point of this
 * panel is that the OCPP traffic is real.
 *
 * It is a dark terminal, but it carries the same card chrome (rounded border,
 * shadow, sentence-case title) as every other panel on the dashboard.
 */

export interface OcppFrame {
  /** ISO-8601 timestamp stamped by the CSMS on the simulated clock. */
  ts: string;
  /** "in" = charge point → CSMS, "out" = CSMS → charge point. */
  direction: string;
  ocpp_id: string;
  /** The raw JSON frame, e.g. `[2,"<uid>","MeterValues",{…}]`. */
  frame: string;
}

export interface OcppLogProps {
  frames?: OcppFrame[] | null;
  /** Rows to show. Hard-capped at 50 (the spec's cap). */
  limit?: number;
  /** Cap for the scroll area when the parent does not set a height. */
  maxHeightPx?: number;
  /** Alias of `maxHeightPx` — the dashboard passes the scroll area's height under this name. */
  heightPx?: number;
}

const MAX_ROWS = 50;
const TZ = 'Asia/Kolkata';

const timeFmt = new Intl.DateTimeFormat('en-GB', {
  timeZone: TZ,
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
});

function parseTs(value: string): number | null {
  const ms = Date.parse(value);
  return Number.isNaN(ms) ? null : ms;
}

interface ParsedFrame {
  /** OCPP message-type id: 2 = Call, 3 = CallResult, 4 = CallError. */
  kind: number | null;
  /** The call's unique id, used to label a result with the action it answers. */
  uid: string | null;
  action: string | null;
}

function parseFrame(raw: string): ParsedFrame {
  try {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return { kind: null, uid: null, action: null };
    const kind = typeof parsed[0] === 'number' ? parsed[0] : null;
    const uid = typeof parsed[1] === 'string' ? parsed[1] : null;
    const action = kind === 2 && typeof parsed[2] === 'string' ? parsed[2] : null;
    return { kind, uid, action };
  } catch {
    return { kind: null, uid: null, action: null };
  }
}

function labelFor(kind: number | null, action: string | null, answers: string | null): string {
  if (kind === 2) return action ?? 'Call';
  if (kind === 3) return answers ? `Result · ${answers}` : 'Result';
  if (kind === 4) return answers ? `Error · ${answers}` : 'Error';
  return 'frame';
}

interface Row {
  key: string;
  time: string;
  inbound: boolean;
  ocppId: string;
  label: string;
  frame: string;
  isError: boolean;
}

export default function OcppLog({
  frames,
  limit = MAX_ROWS,
  maxHeightPx,
  heightPx,
}: OcppLogProps) {
  const scrollCapPx = maxHeightPx ?? heightPx ?? 320;
  const rows = useMemo<Row[]>(() => {
    const items = frames ?? [];

    // The API already returns newest first; re-sort defensively, but only when
    // every timestamp parses, so a surprise format can never scramble the order.
    const stamps = items.map((item) => parseTs(item.ts));
    let ordered = items;
    if (stamps.length > 0 && stamps.every((ms) => ms != null)) {
      ordered = items
        .map((item, i) => ({ item, ms: stamps[i] as number }))
        .sort((a, b) => b.ms - a.ms)
        .map((entry) => entry.item);
    }

    // Action names for CallResult / CallError frames, from the matching Call.
    const actionByUid = new Map<string, string>();
    for (const item of ordered) {
      const { uid, action } = parseFrame(item.frame);
      if (uid && action && !actionByUid.has(uid)) actionByUid.set(uid, action);
    }

    const capped = ordered.slice(0, Math.max(0, Math.min(limit, MAX_ROWS)));
    return capped.map((item, i) => {
      const { kind, uid, action } = parseFrame(item.frame);
      const ms = parseTs(item.ts);
      return {
        key: `${item.ts}|${item.ocpp_id}|${i}`,
        time: ms == null ? item.ts : timeFmt.format(new Date(ms)),
        inbound: item.direction === 'in',
        ocppId: item.ocpp_id,
        label: labelFor(kind, action, uid ? actionByUid.get(uid) ?? null : null),
        frame: item.frame,
        isError: kind === 4,
      };
    });
  }, [frames, limit]);

  return (
    <div className="flex h-full w-full flex-col overflow-hidden rounded-xl border border-slate-800 bg-slate-950 shadow-sm">
      <header className="flex shrink-0 flex-wrap items-baseline justify-between gap-x-4 gap-y-0.5 border-b border-slate-800 px-4 py-2.5">
        <h2 className="text-sm font-semibold text-slate-100">OCPP 1.6-J frames</h2>
        <p
          className="font-mono text-[10px] text-slate-400"
          title="Frames exactly as they went over the websocket. Times are stamped by the CSMS on the simulated clock, shown in Asia/Kolkata."
        >
          <span className="font-semibold text-cyan-300">◀ IN</span> charge point → CSMS ·{' '}
          <span className="font-semibold text-amber-300">OUT ▶</span> CSMS → charge point · newest
          first · <span className="tabular-nums">{rows.length}</span>/{MAX_ROWS}
        </p>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto" style={{ maxHeight: scrollCapPx }}>
        {rows.length === 0 ? (
          <p className="space-y-1 px-3 py-6 text-center font-mono text-xs text-slate-500">
            {/*
              True whether the buffer was just cleared by a demo reset or no charge point has ever
              connected: this component only receives the frames, so it cannot tell the two apart.
              An idle site's next frame is a 30 s heartbeat, so the panel can sit here for a while
              after a reset — it must not read as a connection failure.
            */}
            <span className="block text-slate-400">Log clear — no frames since it was last cleared.</span>
            <span className="block text-slate-600">The next frames appear as the charge points report in.</span>
          </p>
        ) : (
          <ul className="divide-y divide-slate-800/70">
            {rows.map((row) => (
              <li
                key={row.key}
                className={`border-l-2 px-2.5 py-1 ${
                  row.inbound ? 'border-cyan-400/80' : 'border-amber-400/80'
                }`}
              >
                <div className="flex flex-wrap items-baseline gap-x-2 font-mono text-[10px] leading-tight">
                  <span className="tabular-nums text-slate-500">{row.time}</span>
                  <span
                    className={
                      row.inbound ? 'font-semibold text-cyan-300' : 'font-semibold text-amber-300'
                    }
                  >
                    {row.inbound ? '◀ IN ' : 'OUT ▶'}
                  </span>
                  <span className="text-slate-300">{row.ocppId}</span>
                  <span className={row.isError ? 'text-red-400' : 'text-slate-400'}>{row.label}</span>
                </div>
                <pre
                  className={`mt-0.5 whitespace-pre-wrap break-all font-mono text-[10px] leading-tight ${
                    row.isError ? 'text-red-300' : row.inbound ? 'text-cyan-100/90' : 'text-amber-100/90'
                  }`}
                >
                  {row.frame}
                </pre>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
