import { useInfiniteQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import type { CursorPage } from "../api/types";

interface CursorPaginationOptions<T> {
  queryKey: readonly unknown[];
  queryFn: (cursor: string | undefined, signal: AbortSignal) => Promise<CursorPage<T>>;
  enabled?: boolean;
}

/**
 * SPEC-36 §2 cursor pagination. The opaque `next_cursor` is passed straight
 * back to the API; rows are never addressed by offset, so a deployment arriving
 * between pages cannot cause a skip or duplicate.
 */
export function useCursorPagination<T>({
  queryKey,
  queryFn,
  enabled = true,
}: CursorPaginationOptions<T>) {
  const query = useInfiniteQuery({
    queryKey,
    queryFn: ({ pageParam, signal }) => queryFn(pageParam, signal),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    enabled,
  });

  const items = useMemo(
    () => query.data?.pages.flatMap((page) => page.items) ?? [],
    [query.data],
  );

  return {
    ...query,
    items,
    hasMore: query.hasNextPage,
    loadMore: () => {
      void query.fetchNextPage();
    },
    resetPages: () => {
      void query.refetch();
    },
  };
}
