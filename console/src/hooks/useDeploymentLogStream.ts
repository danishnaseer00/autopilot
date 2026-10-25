import { useCallback, useEffect, useRef, useState } from "react";
import { deploymentsApi } from "../api/endpoints";
import type { LogEvent, LogStream } from "../api/types";

export type LogStreamState = "connecting" | "open" | "reconnecting" | "closed";

export interface LogBuffer {
  lines: LogEvent[];
  lastSeq: number;
  truncated: boolean;
  gap: boolean;
}

export const LOG_BUFFER_LIMIT = 5000;

export function emptyLogBuffer(): LogBuffer {
  return { lines: [], lastSeq: 0, truncated: false, gap: false };
}

/**
 * Pure reducer for the log buffer. It drops duplicate sequences (which is what a
 * reconnect can replay), flags any real gap, and keeps at most `limit` lines.
 * SPEC-37 R-10/R-11, AC-4/AC-5.
 */
export function appendLogEvent(
  buffer: LogBuffer,
  event: LogEvent,
  limit = LOG_BUFFER_LIMIT,
): LogBuffer {
  if (!Number.isFinite(event.seq) || event.seq <= buffer.lastSeq) return buffer;
  const gap = buffer.lastSeq > 0 && event.seq > buffer.lastSeq + 1;
  const lines = [...buffer.lines, event];
  const overflow = Math.max(0, lines.length - limit);
  return {
    lines: overflow > 0 ? lines.slice(overflow) : lines,
    lastSeq: event.seq,
    truncated: buffer.truncated || overflow > 0,
    gap: buffer.gap || gap,
  };
}

interface UseDeploymentLogStreamOptions {
  enabled?: boolean;
  /** Test seam; production uses a bounded exponential backoff. */
  reconnectDelayMs?: number;
}

/**
 * SPEC-37 R-9/R-10. The stream is opened with the last sequence number seen,
 * never "now", so a dropped connection cannot lose the middle of a build log.
 * The server contract is `GET /deployments/{id}/logs?from=<seq>`.
 */
export function useDeploymentLogStream(
  deploymentId: string,
  { enabled = true, reconnectDelayMs = 1000 }: UseDeploymentLogStreamOptions = {},
) {
  const [buffer, setBuffer] = useState<LogBuffer>(emptyLogBuffer);
  const [streamState, setStreamState] = useState<LogStreamState>(
    enabled ? "connecting" : "closed",
  );
  const bufferRef = useRef<LogBuffer>(buffer);

  const updateBuffer = useCallback((event: LogEvent) => {
    const next = appendLogEvent(bufferRef.current, event);
    if (next === bufferRef.current) return;
    bufferRef.current = next;
    setBuffer(next);
  }, []);

  useEffect(() => {
    const fresh = emptyLogBuffer();
    bufferRef.current = fresh;
    setBuffer(fresh);
    setStreamState(enabled ? "connecting" : "closed");
  }, [deploymentId, enabled]);

  useEffect(() => {
    if (!enabled) return;

    let disposed = false;
    let stream: LogStream | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;

    const scheduleReconnect = () => {
      if (disposed) return;
      attempt += 1;
      setStreamState("reconnecting");
      const delay = Math.min(15_000, reconnectDelayMs * 2 ** Math.min(attempt - 1, 4));
      retryTimer = setTimeout(connect, delay);
    };

    function connect() {
      if (disposed) return;
      setStreamState(attempt === 0 ? "connecting" : "reconnecting");
      try {
        stream = deploymentsApi.logs(deploymentId, bufferRef.current.lastSeq, {
          onEvent: (event) => {
            if (disposed) return;
            updateBuffer(event);
          },
          onStateChange: (state) => {
            if (disposed) return;
            if (state === "open") {
              attempt = 0;
              setStreamState("open");
            } else if (state === "closed") {
              setStreamState("closed");
            } else if (state === "reconnecting") {
              setStreamState("reconnecting");
            }
          },
          onError: () => {
            if (disposed) return;
            stream?.close();
            scheduleReconnect();
          },
        });
      } catch {
        scheduleReconnect();
      }
    }

    connect();

    return () => {
      disposed = true;
      if (retryTimer) clearTimeout(retryTimer);
      stream?.close();
    };
  }, [deploymentId, enabled, reconnectDelayMs, updateBuffer]);

  return { ...buffer, streamState };
}
