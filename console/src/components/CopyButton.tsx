import { useEffect, useState } from "react";

type CopyStatus = "idle" | "copied" | "failed";

export function CopyButton({
  value,
  label = "Copy",
  copiedLabel = "Copied",
}: {
  value: string;
  label?: string;
  copiedLabel?: string;
}) {
  const [status, setStatus] = useState<CopyStatus>("idle");

  useEffect(() => {
    if (status === "idle") return;
    const timer = setTimeout(() => setStatus("idle"), 3000);
    return () => clearTimeout(timer);
  }, [status]);

  async function copy() {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(value);
      setStatus("copied");
    } catch {
      setStatus("failed");
    }
  }

  return (
    <span className="copy-button">
      <button type="button" className="button button--secondary button--sm" onClick={() => void copy()}>
        {label}
      </button>
      <span className="copy-button__status" role="status" aria-live="polite">
        {status === "copied" ? copiedLabel : null}
        {status === "failed" ? "Copy failed. Select the value and copy it manually." : null}
      </span>
    </span>
  );
}
