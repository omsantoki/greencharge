/**
 * useLiveData — poll one API endpoint on a timer and hand the component the last good value.
 *
 * Rules it enforces, from the build spec and the implementation contract:
 *  - never poll faster than every 3 s (`intervalMs` is clamped to MIN_POLL_INTERVAL_MS);
 *  - never let two requests overlap — the next fetch is scheduled only after the previous
 *    one settles, so a slow backend cannot pile up requests;
 *  - keep the last good data when a request fails and raise `stale` instead of blanking the
 *    dashboard (the operator keeps seeing numbers, clearly marked as not fresh);
 *  - clear the timer, abort the in-flight request and update no state after unmount.
 *
 * The fetcher is read through a ref, so an inline arrow may be passed without restarting the
 * loop — but that also means a fetcher that closes over a changing value (a session id, say)
 * is only picked up by the NEXT poll. To switch immediately, call `refresh()` after the
 * change, or give the component a `key` so it remounts.
 */
import { useCallback, useEffect, useRef, useState } from 'react';

/** The spec forbids polling more often than every 3 seconds. */
export const MIN_POLL_INTERVAL_MS = 3000;

/** A fetcher may accept the hook's AbortSignal; `() => getSites()` is equally fine. */
export type LiveFetcher<T> = (signal?: AbortSignal) => Promise<T>;

export type LiveData<T> = {
  /** The last value that loaded successfully, or null before the first one arrives. */
  data: T | null;
  /** The most recent failure, cleared by the next success. */
  error: Error | null;
  /** True only until the first attempt settles — polling never flips it back on, so nothing flickers. */
  loading: boolean;
  /** True when `data` is being shown despite the latest attempt having failed. */
  stale: boolean;
  /** Fetch now. Safe to call at any time: it replaces the pending timer and never overlaps a request. */
  refresh: () => void;
};

type State<T> = {
  data: T | null;
  error: Error | null;
  loading: boolean;
  stale: boolean;
};

const INITIAL_STATE = { data: null, error: null, loading: true, stale: false };

function clampInterval(intervalMs: number): number {
  if (!Number.isFinite(intervalMs)) return MIN_POLL_INTERVAL_MS;
  return Math.max(MIN_POLL_INTERVAL_MS, intervalMs);
}

function toError(err: unknown): Error {
  return err instanceof Error ? err : new Error(String(err));
}

export function useLiveData<T>(
  fetcher: LiveFetcher<T>,
  intervalMs: number = MIN_POLL_INTERVAL_MS,
): LiveData<T> {
  const interval = clampInterval(intervalMs);

  const [state, setState] = useState<State<T>>(INITIAL_STATE as State<T>);

  // The fetcher is usually an inline arrow, so it is read through a ref: a new identity on
  // every render must not restart the polling loop.
  const fetcherRef = useRef<LiveFetcher<T>>(fetcher);
  const intervalRef = useRef<number>(interval);
  const mountedRef = useRef<boolean>(false);
  const inFlightRef = useRef<boolean>(false);
  const refreshPendingRef = useRef<boolean>(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const abortTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const controllerRef = useRef<AbortController | null>(null);
  const runRef = useRef<() => void>(() => undefined);

  useEffect(() => {
    fetcherRef.current = fetcher;
    intervalRef.current = interval;
  });

  const clearTimer = useCallback(() => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const schedule = useCallback(() => {
    clearTimer();
    timerRef.current = setTimeout(() => {
      timerRef.current = null;
      runRef.current();
    }, intervalRef.current);
  }, [clearTimer]);

  const run = useCallback(async () => {
    if (!mountedRef.current) return;
    // A request is already out: remember that another one was asked for and run it after.
    if (inFlightRef.current) {
      refreshPendingRef.current = true;
      return;
    }

    inFlightRef.current = true;
    const controller = new AbortController();
    controllerRef.current = controller;

    try {
      const value = await fetcherRef.current(controller.signal);
      if (!mountedRef.current || controller.signal.aborted) return;
      setState({ data: value, error: null, loading: false, stale: false });
    } catch (err) {
      if (!mountedRef.current || controller.signal.aborted) return;
      const error = toError(err);
      setState((prev) => ({
        data: prev.data,
        error,
        loading: false,
        stale: prev.data !== null,
      }));
    } finally {
      // Only the request that is still the current one releases the lock and schedules the
      // next tick; a request left over from a previous mount must not.
      if (controllerRef.current === controller) {
        inFlightRef.current = false;
        controllerRef.current = null;
        if (mountedRef.current) {
          // An aborted request delivered nothing, so a component that is still mounted
          // (an abort raced a remount) needs its value now rather than one interval later.
          const abortedWhileMounted = controller.signal.aborted;
          if (refreshPendingRef.current || abortedWhileMounted) {
            refreshPendingRef.current = false;
            runRef.current();
          } else {
            schedule();
          }
        }
      }
    }
  }, [schedule]);

  useEffect(() => {
    runRef.current = () => {
      void run();
    };
  }, [run]);

  useEffect(() => {
    mountedRef.current = true;
    if (abortTimerRef.current !== null) {
      // React StrictMode remounted us in the same tick: adopt the request that is already
      // out instead of aborting it and paying for a second one.
      clearTimeout(abortTimerRef.current);
      abortTimerRef.current = null;
    }
    // When a request is still in flight it will deliver its value and schedule the next poll.
    if (!inFlightRef.current) void run();

    return () => {
      mountedRef.current = false;
      clearTimer();
      refreshPendingRef.current = false;
      const controller = controllerRef.current;
      if (controller) {
        // Deferred by one tick so a StrictMode remount can cancel it; a real unmount aborts
        // the in-flight request immediately afterwards.
        abortTimerRef.current = setTimeout(() => {
          abortTimerRef.current = null;
          controller.abort();
        }, 0);
      }
    };
  }, [run, clearTimer]);

  // A changed interval re-arms a timer that is already waiting; an in-flight request picks
  // the new interval up when it schedules the next one.
  useEffect(() => {
    if (timerRef.current !== null) schedule();
  }, [interval, schedule]);

  const refresh = useCallback(() => {
    clearTimer();
    runRef.current();
  }, [clearTimer]);

  return { data: state.data, error: state.error, loading: state.loading, stale: state.stale, refresh };
}

export default useLiveData;
