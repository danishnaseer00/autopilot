import { useEffect } from "react";

export function useDocumentTitle(title: string | null | undefined): void {
  useEffect(() => {
    const previous = document.title;
    document.title = title ? `${title} · AutoPilot Console` : "AutoPilot Console";
    return () => {
      document.title = previous;
    };
  }, [title]);
}
