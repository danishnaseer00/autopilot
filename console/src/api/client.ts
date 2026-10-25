import { apiErrorFromResponse, NetworkError } from "./errors";
import { API_BASE, paths, type HttpMethod } from "./contract";
import type { LogEvent, LogStream, LogStreamHandlers } from "./types";

export interface RequestOptions {
  query?: Record<string, string | number | boolean | undefined | null>;
  body?: unknown;
  signal?: AbortSignal;
  headers?: Record<string, string>;
}

export interface Transport {
  request: (method: HttpMethod, path: string, init: RequestInit) => Promise<Response>;
}

/** True only when explicitly enabled. Vite dev defaults to the real API. */
export function isMockMode(): boolean {
  const flag = import.meta.env.VITE_USE_MOCKS;
  return flag === "true" || import.meta.env.MODE === "test";
}

/**
 * Local-only developer login. The control API exposes this route only when
 * APP_ENV_NAME=dev and DEV_LOGIN_ENABLED=true; a production build never
 * enables the button.
 */
export function isDevLoginEnabled(): boolean {
  return import.meta.env.DEV === true && import.meta.env.VITE_DEV_LOGIN !== "false";
}

export function buildUrl(
  path: string,
  query?: RequestOptions["query"],
): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined && value !== null && value !== "") {
      search.set(key, String(value));
    }
  }
  const suffix = search.size > 0 ? `?${search.toString()}` : "";
  return `${API_BASE}${path}${suffix}`;
}

/**
 * Cookie-only browser transport. SPEC-36 §2 / SPEC-05 R-1: the session is an
 * HttpOnly cookie. This function never reads or attaches a token, and nothing
 * in the bundle can persist one.
 */
const httpTransport: Transport = {
  request: (method, url, init) =>
    fetch(url, {
      ...init,
      method,
      credentials: "include",
      headers: init.headers,
    }),
};

let transportPromise: Promise<Transport> | null = null;

async function getTestTransport(): Promise<Transport> {
  const module = await import("./mocks/mockTransport");
  return module.mockTransport;
}

export async function getTransport(): Promise<Transport> {
  if (!isMockMode()) return httpTransport;
  transportPromise ??= getTestTransport();
  return transportPromise;
}

function newRequestId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `req-${Math.random().toString(36).slice(2)}`;
}

/**
 * One request through the typed contract layer. Errors are converted to
 * ApiError/NetworkError here; no caller sees a raw Response or TypeError.
 */
export async function request<T>(
  method: HttpMethod,
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const url = buildUrl(path, options.query);
  const headers = new Headers({ Accept: "application/json", "X-Request-Id": newRequestId() });
  let body: BodyInit | undefined;

  if (options.body !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(options.body);
  }
  for (const [key, value] of Object.entries(options.headers ?? {})) {
    headers.set(key, value);
  }

  const transport = await getTransport();
  let response: Response;
  try {
    response = await transport.request(method, url, {
      method,
      headers,
      body,
      signal: options.signal,
      credentials: "include",
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new NetworkError();
  }

  if (!response.ok) {
    throw await apiErrorFromResponse(response);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  try {
    return (await response.json()) as T;
  } catch {
    throw new NetworkError("response was not valid JSON");
  }
}

type LogStreamFactory = (
  deploymentId: string,
  from: number,
  handlers: LogStreamHandlers,
) => LogStream;

let injectedLogStreamFactory: LogStreamFactory | null = null;

/** Test seam: the hook stays deterministic without a real EventSource. */
export function setLogStreamFactory(factory: LogStreamFactory | null): void {
  injectedLogStreamFactory = factory;
}

export function createLogStream(
  deploymentId: string,
  from: number,
  handlers: LogStreamHandlers,
): LogStream {
  if (injectedLogStreamFactory) return injectedLogStreamFactory(deploymentId, from, handlers);
  if (isMockMode()) {
    let closed = false;
    const stream: LogStream = {
      close: () => {
        closed = true;
      },
    };
    void import("./mocks/mockLogs").then((module) => {
      if (closed) return;
      module.startMockLogStream(deploymentId, from, handlers);
    });
    return stream;
  }
  return startEventSourceStream(deploymentId, from, handlers);
}

/**
 * EventSource reconnects to the original URL, which would replay from the
 * original `from`. We close it on error and reconnect with the latest
 * sequence, satisfying SPEC-36 R-1 / SPEC-37 R-10.
 */
function startEventSourceStream(
  deploymentId: string,
  from: number,
  handlers: LogStreamHandlers,
): LogStream {
  let closed = false;
  let latest = from;
  let source: EventSource | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let attempt = 0;

  const open = (seq: number) => {
    if (closed) return;
    handlers.onStateChange?.("connecting");
    const url = buildUrl(paths.deploymentLogs(deploymentId), { from: seq });
    source = new EventSource(url, { withCredentials: true });

    source.onopen = () => {
      attempt = 0;
      handlers.onStateChange?.("open");
    };

    source.onmessage = (message: MessageEvent<string>) => {
      try {
        const event = JSON.parse(message.data) as LogEvent;
        if (typeof event.seq !== "number" || typeof event.line !== "string") return;
        latest = Math.max(latest, event.seq);
        handlers.onEvent(event);
      } catch {
        // A malformed frame is ignored; the next sequence resumes normally.
      }
    };

    source.onerror = () => {
      if (closed) return;
      source?.close();
      source = null;
      handlers.onStateChange?.("reconnecting");
      handlers.onError?.();
      attempt += 1;
      const delay = Math.min(15_000, 500 * 2 ** Math.min(attempt, 5));
      timer = setTimeout(() => open(latest), delay);
    };
  };

  open(from);

  return {
    close: () => {
      closed = true;
      if (timer) clearTimeout(timer);
      source?.close();
      source = null;
      handlers.onStateChange?.("closed");
    },
  };
}
