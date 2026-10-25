import type { ReactNode } from "react";

export function EmptyState({
  title,
  children,
  action,
  eyebrow,
}: {
  title: string;
  children: ReactNode;
  action?: ReactNode;
  eyebrow?: string;
}) {
  return (
    <section className="empty-state" aria-labelledby="empty-state-title">
      <div className="empty-state__mark" aria-hidden="true">
        ◇
      </div>
      {eyebrow ? <p className="eyebrow">{eyebrow}</p> : null}
      <h2 id="empty-state-title">{title}</h2>
      <div className="empty-state__body">{children}</div>
      {action ? <div className="empty-state__actions">{action}</div> : null}
    </section>
  );
}
