import { Component, type ErrorInfo, type ReactNode } from "react";

interface ErrorBoundaryState {
  failed: boolean;
}

/**
 * SPEC-37 R-5/R-7: a render failure lands on a designed fallback, never a stack
 * trace or the raw error object.
 */
export class ErrorBoundary extends Component<{ children: ReactNode }, ErrorBoundaryState> {
  override state: ErrorBoundaryState = { failed: false };

  static getDerivedStateFromError(): ErrorBoundaryState {
    return { failed: true };
  }

  override componentDidCatch(_error: Error, _info: ErrorInfo): void {
    // A real deployment reports this to the platform's error sink. The console
    // deliberately does not print the error into the DOM.
  }

  override render(): ReactNode {
    if (this.state.failed) {
      return (
        <div className="error-page">
          <div className="error-page__card">
            <p className="eyebrow">Console error</p>
            <h1>This screen could not be displayed</h1>
            <p>
              The console hit an unexpected problem rendering this view. Nothing you had saved was
              changed. Reload the page to try again.
            </p>
            <button
              type="button"
              className="button button--primary button--md"
              onClick={() => window.location.reload()}
            >
              Reload page
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
