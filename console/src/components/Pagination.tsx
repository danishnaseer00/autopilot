import { Button } from "./Button";
import { pluralize } from "../lib/format";

export function Pagination({
  shown,
  hasMore,
  loading,
  onLoadMore,
  noun = "item",
}: {
  shown: number;
  hasMore: boolean;
  loading: boolean;
  onLoadMore: () => void;
  noun?: string;
}) {
  return (
    <div className="pagination">
      <p className="pagination__count" role="status" aria-live="polite">
        Showing {pluralize(shown, noun)}
        {hasMore ? " so far" : ""}
      </p>
      {hasMore ? (
        <Button onClick={onLoadMore} busy={loading}>
          Load more
        </Button>
      ) : null}
    </div>
  );
}
