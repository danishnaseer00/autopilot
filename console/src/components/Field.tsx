import { useId, type InputHTMLAttributes, type ReactNode, type SelectHTMLAttributes, type TextareaHTMLAttributes } from "react";

interface FieldChrome {
  label: string;
  hint?: string;
  error?: string;
  required?: boolean;
}

function describedBy(id: string, hint?: string, error?: string): string | undefined {
  const ids = [hint ? `${id}-hint` : null, error ? `${id}-error` : null].filter(Boolean);
  return ids.length > 0 ? ids.join(" ") : undefined;
}

function Chrome({
  id,
  label,
  hint,
  error,
  required,
  children,
}: FieldChrome & { id: string; children: ReactNode }) {
  return (
    <div className={`field${error ? " field--invalid" : ""}`}>
      <label className="field__label" htmlFor={id}>
        {label}
        {required ? (
          <span className="field__required" aria-hidden="true">
            *
          </span>
        ) : null}
      </label>
      {children}
      {hint ? (
        <p className="field__hint" id={`${id}-hint`}>
          {hint}
        </p>
      ) : null}
      {error ? (
        <p className="field__error" id={`${id}-error`} role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}

interface TextInputProps extends Omit<InputHTMLAttributes<HTMLInputElement>, "id">, FieldChrome {
  id?: string;
}

export function TextInput({ id: providedId, label, hint, error, required, ...rest }: TextInputProps) {
  const generated = useId();
  const id = providedId ?? generated;
  return (
    <Chrome id={id} label={label} hint={hint} error={error} required={required}>
      <input
        {...rest}
        id={id}
        required={required}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy(id, hint, error)}
        className="input"
      />
    </Chrome>
  );
}

interface TextAreaProps
  extends Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, "id">,
    FieldChrome {
  id?: string;
}

export function TextArea({ id: providedId, label, hint, error, required, ...rest }: TextAreaProps) {
  const generated = useId();
  const id = providedId ?? generated;
  return (
    <Chrome id={id} label={label} hint={hint} error={error} required={required}>
      <textarea
        {...rest}
        id={id}
        required={required}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy(id, hint, error)}
        className="input textarea"
      />
    </Chrome>
  );
}

interface SelectProps extends Omit<SelectHTMLAttributes<HTMLSelectElement>, "id">, FieldChrome {
  id?: string;
  children: ReactNode;
}

export function SelectField({
  id: providedId,
  label,
  hint,
  error,
  required,
  children,
  ...rest
}: SelectProps) {
  const generated = useId();
  const id = providedId ?? generated;
  return (
    <Chrome id={id} label={label} hint={hint} error={error} required={required}>
      <select
        {...rest}
        id={id}
        required={required}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy(id, hint, error)}
        className="input select"
      >
        {children}
      </select>
    </Chrome>
  );
}

/** Checkbox whose label is the description, including an explicit consequence. */
interface CheckboxProps {
  id?: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
  label: ReactNode;
  hint?: string;
  error?: string;
  disabled?: boolean;
}

export function Checkbox({ id: providedId, checked, onChange, label, hint, error, disabled }: CheckboxProps) {
  const generated = useId();
  const id = providedId ?? generated;
  return (
    <div className={`checkbox${error ? " checkbox--invalid" : ""}`}>
      <input
        id={id}
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy(id, hint, error)}
      />
      <label htmlFor={id}>
        <span>{label}</span>
        {hint ? (
          <span className="checkbox__hint" id={`${id}-hint`}>
            {hint}
          </span>
        ) : null}
        {error ? (
          <span className="field__error" id={`${id}-error`} role="alert">
            {error}
          </span>
        ) : null}
      </label>
    </div>
  );
}
