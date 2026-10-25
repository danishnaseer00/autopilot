const DATE_TIME = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});

const DATE_ONLY = new Intl.DateTimeFormat(undefined, { dateStyle: "medium" });

const RELATIVE = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });

function parse(value: string | null | undefined): Date | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatDateTime(value: string | null | undefined): string {
  const date = parse(value);
  return date ? DATE_TIME.format(date) : "Not recorded";
}

export function formatDate(value: string | null | undefined): string {
  const date = parse(value);
  return date ? DATE_ONLY.format(date) : "Not recorded";
}

/** Human duration inside a deployment, in seconds. */
export function formatDuration(
  start: string | null | undefined,
  end: string | null | undefined,
): string {
  const from = parse(start);
  if (!from) return "Not started";
  const to = parse(end) ?? new Date();
  return formatSeconds(Math.max(0, Math.round((to.getTime() - from.getTime()) / 1000)));
}

export function formatSeconds(totalSeconds: number): string {
  if (!Number.isFinite(totalSeconds) || totalSeconds < 0) return "Unknown";
  const seconds = Math.round(totalSeconds);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  if (minutes < 60) return remainder ? `${minutes}m ${remainder}s` : `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const restMinutes = minutes % 60;
  return restMinutes ? `${hours}h ${restMinutes}m` : `${hours}h`;
}

export function formatRelative(value: string | null | undefined): string {
  const date = parse(value);
  if (!date) return "Not recorded";
  const diffSeconds = Math.round((date.getTime() - Date.now()) / 1000);
  const table: Array<[Intl.RelativeTimeFormatUnit, number]> = [
    ["year", 31_536_000],
    ["month", 2_592_000],
    ["week", 604_800],
    ["day", 86_400],
    ["hour", 3_600],
    ["minute", 60],
  ];
  for (const [unit, size] of table) {
    if (Math.abs(diffSeconds) >= size) {
      return RELATIVE.format(Math.round(diffSeconds / size), unit);
    }
  }
  return RELATIVE.format(diffSeconds, "second");
}

export function formatMoney(cents: number, currency: string): string {
  try {
    return new Intl.NumberFormat(undefined, {
      style: "currency",
      currency,
    }).format(cents / 100);
  } catch {
    return `${(cents / 100).toFixed(2)} ${currency}`;
  }
}

export function pluralize(count: number, singular: string, plural = `${singular}s`): string {
  return `${count} ${count === 1 ? singular : plural}`;
}

/** A stable, human-readable label from an API enum without trusting arbitrary markup. */
export function humanize(value: string): string {
  return value
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}
