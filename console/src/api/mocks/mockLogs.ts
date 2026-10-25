import type { LogStream, LogStreamHandlers } from "../types";

/**
 * Deterministic development stream for `GET /deployments/{id}/logs`.
 *
 * SPEC-36 R-1 makes `from` a monotonic sequence. This mock obeys the same
 * contract: subscribing with `from=12` starts at 13, so a reconnect can be
 * exercised end to end without a real server.
 */
const TOTAL_LINES = 240;

const LINES = [
  "> autopilot build --target production",
  "Resolved manifest sha256:3b41f5c2d0a9",
  "docker build -f Dockerfile .",
  "Step 1/9 : FROM node:22-alpine",
  "Step 2/9 : WORKDIR /app",
  "Step 3/9 : COPY package*.json ./",
  "Step 4/9 : RUN npm ci --omit=dev",
  "npm warn deprecated inflight@1.0.6: This module is not supported",
  "added 412 packages in 18s",
  "Step 5/9 : COPY . .",
  "Step 6/9 : RUN npm run build",
  "vite v5.4.11 building for production...",
  "transforming modules...",
  "dist/index.html                  0.52 kB",
  "dist/assets/index-4f2a.js      184.21 kB",
  "Step 7/9 : USER node",
  "Step 8/9 : EXPOSE 8080",
  "Step 9/9 : CMD [\"node\", \"server.js\"]",
  "Successfully built image sha256:6a7824ff801d",
  "Pushing image to registry...",
  "Health check passed on port 8080",
  "Promoting revision to production",
];

export function lineAt(deploymentId: string, seq: number): string {
  const base = LINES[(seq - 1) % LINES.length] ?? "build output";
  return `[${deploymentId}] [${String(seq).padStart(5, "0")}] ${base}`;
}

export function startMockLogStream(
  deploymentId: string,
  from: number,
  handlers: LogStreamHandlers,
): LogStream {
  let seq = Math.max(0, from);
  let closed = false;
  handlers.onStateChange?.("connecting");

  const timer = setInterval(() => {
    if (closed) return;
    handlers.onStateChange?.("open");
    seq += 1;
    handlers.onEvent({
      seq,
      line: lineAt(deploymentId, seq),
      at: new Date().toISOString(),
    });
    if (seq >= TOTAL_LINES) {
      clearInterval(timer);
      handlers.onStateChange?.("closed");
    }
  }, 120);

  return {
    close: () => {
      closed = true;
      clearInterval(timer);
      handlers.onStateChange?.("closed");
    },
  };
}
