import { request } from "../client";
import { paths } from "../contract";
import type { PdfJob, Report, ReportPeriod } from "../types";

/** SPEC-36 §3.6 — reports (SPEC-22). Shell-only in this slice. */
export const reportsApi = {
  get: (period: ReportPeriod, signal?: AbortSignal) =>
    request<Report>("GET", paths.report(period), { signal }),
  pdf: (period: ReportPeriod) => request<PdfJob>("GET", paths.reportPdf(period)),
};
