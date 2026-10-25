import { useEffect, useId, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { isApiError, type ErrorCode } from "../api/errors";
import { ERROR_UI, FALLBACK_ERROR_UI, type ErrorUiDescriptor } from "../api/errorPresentation";
import { useAuth } from "../auth/AuthContext";
import { formatDateTime } from "../lib/format";
import { traceId } from "../lib/problem";
import { buttonClass } from "./Button";
import { CopyButton } from "./CopyButton";

interface ProblemViewProps {
  error: unknown;
  onRetry?: () => void;
  onReload?: () => void;
  compact?: boolean;
}

function descriptorFor(code: string | undefined): ErrorUiDescriptor {
  return code && code in ERROR_UI ? ERROR_UI[code as ErrorCode] : FALLBACK_ERROR_UI;
}

function DnsRecord({ record }: { record: { type: string; name: string; value: string } }) {
  return (
    <div className="dns-record">
      <dl className="definition-list">
        <div>
          <dt>Type</dt>
          <dd>
            <code>{record.type}</code>
          </dd>
        </div>
        <div>
          <dt>Name</dt>
          <dd>
            <code>{record.name}</code>
          </dd>
        </div>
        <div>
          <dt>Value</dt>
          <dd>
            <code>{record.value}</code>
          </dd>
        </div>
      </dl>
      <CopyButton value={`${record.type} ${record.name} ${record.value}`} label="Copy DNS record" />
    </div>
  );
}

/**
 * SPEC-37 R-6/R-7. Every API error is rendered from the table in
 * `errorPresentation.ts`. The raw ProblemDetails object, server detail text and
 * stack traces are never placed in the DOM; only mapped copy, structured
 * numeric fields and the support trace ID are shown.
 */
export function ProblemView({ error, onRetry, onReload, compact = false }: ProblemViewProps) {
  const auth = useAuth();
  const location = useLocation();
  const titleId = useId();
  const problem = isApiError(error) ? error.problem : null;
  const code = problem?.code;
  const descriptor = descriptorFor(code);
  const fallback = !problem || !(code && code in ERROR_UI);
  const [countdown, setCountdown] = useState(0);

  const isRateLimited = code === "rate_limited";
  const initialCountdown = isRateLimited ? (isApiError(error) ? (error.retryAfterSeconds ?? 30) : 30) : 0;

  useEffect(() => {
    if (!isRateLimited) {
      setCountdown(0);
      return;
    }
    setCountdown(initialCountdown);
    const timer = setInterval(() => {
      setCountdown((current) => (current <= 1 ? 0 : current - 1));
    }, 1000);
    return () => clearInterval(timer);
  }, [initialCountdown, isRateLimited]);

  const retryDisabled = isRateLimited && countdown > 0;
  const trace = traceId(error);
  const next = encodeURIComponent(`${location.pathname}${location.search}`);

  function retryLabel(): string {
    if (isRateLimited && countdown > 0) return `Retry in ${countdown}s`;
    if (descriptor.retry === "backoff") return "Retry now";
    return "Retry";
  }

  return (
    <section
      className={`problem problem--${descriptor.tone}${compact ? " problem--compact" : ""}`}
      aria-labelledby={titleId}
    >
      <p className="problem__eyebrow">{fallback ? "Request failed" : "Request could not be completed"}</p>
      <h2 id={titleId}>{descriptor.title}</h2>
      <p className="problem__description">{descriptor.description}</p>

      {code === "quota_exceeded" && problem?.quota ? (
        <div className="problem__details">
          <dl className="definition-list">
            <div>
              <dt>Plan limit</dt>
              <dd>{problem.quota.limit.toLocaleString()}</dd>
            </div>
            <div>
              <dt>Currently used</dt>
              <dd>{problem.quota.current.toLocaleString()}</dd>
            </div>
            <div>
              <dt>Resets</dt>
              <dd>{formatDateTime(problem.quota.reset_at)}</dd>
            </div>
          </dl>
          <Link className={buttonClass("primary", "sm")} to="/billing">
            Review plans and usage
          </Link>
        </div>
      ) : null}

      {code === "quota_pending" ? (
        <div className="problem__details">
          <p>
            Queue position:{" "}
            <strong>{problem?.queue ? `#${problem.queue.position}` : "not reported"}</strong>
          </p>
          {onRetry ? (
            <button type="button" className={buttonClass("secondary", "sm")} onClick={onRetry}>
              Check queue again
            </button>
          ) : null}
        </div>
      ) : null}

      {code === "domain_unverified" ? (
        <div className="problem__details">
          {problem?.dns_record ? (
            <DnsRecord record={problem.dns_record} />
          ) : (
            <p className="hint">
              The API did not include the DNS record. This is a contract gap; quote the trace ID
              below to support.
            </p>
          )}
        </div>
      ) : null}

      {code === "validation_failed" ? (
        <div className="problem__details">
          {problem?.errors && problem.errors.length > 0 ? (
            <ul className="validation-list">
              {problem.errors.map((issue, index) => (
                <li key={`${issue.path}-${index}`}>
                  <code>{issue.path || "form"}</code>
                  <span>{issue.message}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p>Correct the highlighted fields and submit again.</p>
          )}
        </div>
      ) : null}

      {code === "rate_limited" ? (
        <div className="problem__details">
          {problem?.rate_limit ? (
            <dl className="definition-list">
              <div>
                <dt>Limit</dt>
                <dd>{problem.rate_limit.limit.toLocaleString()} requests</dd>
              </div>
              <div>
                <dt>Remaining</dt>
                <dd>{problem.rate_limit.remaining.toLocaleString()}</dd>
              </div>
              <div>
                <dt>Reset</dt>
                <dd>{formatDateTime(problem.rate_limit.reset_at)}</dd>
              </div>
            </dl>
          ) : null}
          <p className="hint">
            {countdown > 0 ? `You can retry in ${countdown} seconds.` : "The wait has elapsed."}
          </p>
        </div>
      ) : null}

      <div className="problem__actions">
        {descriptor.action === "sign_in" ? (
          <Link className={buttonClass("primary")} to={`/login?next=${next}`}>
            Sign in again
          </Link>
        ) : null}

        {descriptor.action === "sign_out" ? (
          <button type="button" className={buttonClass("secondary")} onClick={auth.signOut}>
            {auth.canSignOut ? "Sign out" : "Leave console"}
          </button>
        ) : null}

        {descriptor.action === "upgrade" ? (
          <Link className={buttonClass("primary")} to="/billing">
            View plans
          </Link>
        ) : null}

        {descriptor.action === "reload" ? (
          <button
            type="button"
            className={buttonClass("primary")}
            onClick={() => (onReload ? onReload() : window.location.reload())}
          >
            Reload current data
          </button>
        ) : null}

        {descriptor.action === "retry" || descriptor.action === "dns" ? (
          onRetry ? (
            <button
              type="button"
              className={buttonClass("primary")}
              onClick={onRetry}
              disabled={retryDisabled}
            >
              {descriptor.action === "dns" ? "Check verification" : retryLabel()}
            </button>
          ) : descriptor.action === "retry" ? (
            <p className="hint">Use the button above when the condition clears.</p>
          ) : null
        ) : null}

        {fallback && onRetry ? (
          <button type="button" className={buttonClass("primary")} onClick={onRetry}>
            Retry
          </button>
        ) : null}
      </div>

      {trace ? (
        <p className="problem__trace">
          Support trace ID: <code>{trace}</code>
        </p>
      ) : null}
    </section>
  );
}

export function ErrorState({
  error,
  onRetry,
  onReload,
  compact,
}: {
  error: unknown;
  onRetry?: () => void;
  onReload?: () => void;
  compact?: boolean;
}) {
  if (isApiError(error)) {
    return <ProblemView error={error} onRetry={onRetry} onReload={onReload} compact={compact} />;
  }

  return (
    <section
      className={`problem problem--danger${compact ? " problem--compact" : ""}`}
      aria-labelledby="network-problem-title"
    >
      <p className="problem__eyebrow">Connection problem</p>
      <h2 id="network-problem-title">Cannot reach the platform</h2>
      <p className="problem__description">
        The request did not receive a response. Nothing was changed. Check your connection and try
        again. Repeated failures will back off automatically.
      </p>
      {onRetry ? (
        <div className="problem__actions">
          <button type="button" className={buttonClass("primary")} onClick={onRetry}>
            Retry
          </button>
        </div>
      ) : null}
    </section>
  );
}
