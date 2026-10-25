export type SessionLostReason = "unauthenticated" | "suspended";

type Listener = (reason: SessionLostReason) => void;

const listeners = new Set<Listener>();

/**
 * SPEC-37 R-6 requires a 401 from any request to preserve the intended route
 * and redirect to login. This tiny event channel lets the query cache tell the
 * auth context without coupling either module to the other.
 */
export function subscribeSessionLost(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function reportSessionLost(reason: SessionLostReason): void {
  for (const listener of listeners) listener(reason);
}
