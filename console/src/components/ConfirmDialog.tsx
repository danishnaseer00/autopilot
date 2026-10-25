import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Button } from "./Button";

export interface ConfirmDialogProps {
  open: boolean;
  title: string;
  description: ReactNode;
  consequence: ReactNode;
  confirmLabel: string;
  cancelLabel?: string;
  tone?: "danger" | "primary";
  busy?: boolean;
  /** When set, the user must type this exact text before confirming. */
  requireText?: string;
  requireTextLabel?: string;
  onConfirm: () => void;
  onCancel: () => void;
}

/**
 * SPEC-37 R-15: destructive confirmation must name the consequence. Focus
 * starts on Cancel, Escape cancels, Tab cycles within the dialog, and focus is
 * restored to the trigger on close.
 */
export function ConfirmDialog({
  open,
  title,
  description,
  consequence,
  confirmLabel,
  cancelLabel = "Cancel",
  tone = "danger",
  busy = false,
  requireText,
  requireTextLabel = "Type the text above to confirm",
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const baseId = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const restoreRef = useRef<HTMLElement | null>(null);
  const cancelHandler = useRef(onCancel);
  const [typed, setTyped] = useState("");

  useEffect(() => {
    cancelHandler.current = onCancel;
  }, [onCancel]);

  useEffect(() => {
    if (!open) {
      setTyped("");
      return;
    }
    restoreRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const focusTarget = requireText ? inputRef.current : cancelRef.current;
    focusTarget?.focus();

    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        cancelHandler.current();
        return;
      }
      if (event.key !== "Tab" || !dialogRef.current) return;
      const focusable = Array.from(
        dialogRef.current.querySelectorAll<HTMLElement>(
          'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      ).filter((element) => !element.hasAttribute("hidden"));
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (!first || !last) return;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      restoreRef.current?.focus();
    };
  }, [open, requireText]);

  if (!open) return null;

  const confirmed = !requireText || typed === requireText;

  return createPortal(
    <div className="dialog-backdrop">
      <div
        ref={dialogRef}
        className="dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={`${baseId}-title`}
        aria-describedby={`${baseId}-body`}
      >
        <h2 id={`${baseId}-title`}>{title}</h2>
        <div className="dialog__body" id={`${baseId}-body`}>
          {description}
          <p className={`dialog__consequence dialog__consequence--${tone}`}>{consequence}</p>
          {requireText ? (
            <div className="field">
              <label className="field__label" htmlFor={`${baseId}-typed`}>
                {requireTextLabel}
              </label>
              <input
                ref={inputRef}
                id={`${baseId}-typed`}
                className="input"
                value={typed}
                autoComplete="off"
                onChange={(event) => setTyped(event.target.value)}
              />
              <p className="field__hint">Type {requireText} exactly.</p>
            </div>
          ) : null}
        </div>
        <div className="dialog__actions">
          <Button ref={cancelRef} onClick={onCancel} busy={busy}>
            {cancelLabel}
          </Button>
          <Button
            variant={tone === "danger" ? "danger" : "primary"}
            onClick={onConfirm}
            disabled={!confirmed}
            busy={busy}
          >
            {confirmLabel}
          </Button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
