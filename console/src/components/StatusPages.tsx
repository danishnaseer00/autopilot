import { Link } from "react-router-dom";
import { buttonClass } from "./Button";
import { useDocumentTitle } from "../hooks/useDocumentTitle";

export function NotFoundPage() {
  useDocumentTitle("Not found");
  return (
    <div className="status-page">
      <p className="eyebrow">404 · Not found</p>
      <h1>This page does not exist</h1>
      <p>
        The address may be wrong, or the item may have been removed. This message is deliberately
        generic: the console does not reveal whether a resource exists in another organisation.
      </p>
      <div className="row-actions">
        <Link className={buttonClass("primary")} to="/projects">
          Go to projects
        </Link>
        <Link className={buttonClass("secondary")} to="/incidents">
          Go to incidents
        </Link>
      </div>
    </div>
  );
}
