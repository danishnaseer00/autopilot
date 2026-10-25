import { type ReactNode } from "react";

export function LoadingPanel({ label = "Loading", lines = 4 }: { label?: string; lines?: number }) {
  return (
    <div className="loading-panel" role="status" aria-live="polite">
      <span className="visually-hidden">{label}…</span>
      <div className="loading-panel__bar loading-panel__bar--title" aria-hidden="true" />
      {Array.from({ length: lines }, (_, index) => (
        <div
          key={index}
          className={`loading-panel__bar loading-panel__bar--line-${(index % 3) + 1}`}
          aria-hidden="true"
        />
      ))}
    </div>
  );
}

export function FullPageLoading({ label = "Loading the console" }: { label?: string }) {
  return (
    <div className="full-page-center">
      <LoadingPanel label={label} lines={5} />
    </div>
  );
}

export function InlineLoading({ children = "Loading" }: { children?: ReactNode }) {
  return (
    <span className="inline-loading" role="status" aria-live="polite">
      {children}…
    </span>
  );
}
