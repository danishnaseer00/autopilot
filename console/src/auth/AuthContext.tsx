import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "../api/endpoints";
import { isDevLoginEnabled, isMockMode } from "../api/client";
import { isApiError } from "../api/errors";
import type { MeResponse, OrgMembership } from "../api/types";
import { subscribeSessionLost } from "./sessionEvents";

export type AuthStatus = "loading" | "authenticated" | "anonymous" | "suspended" | "error";

interface AuthSnapshot {
  status: AuthStatus;
  me: MeResponse | null;
  error: unknown;
}

export interface AuthContextValue extends AuthSnapshot {
  isMock: boolean;
  isDevLogin: boolean;
  canSignOut: boolean;
  orgs: OrgMembership[];
  activeOrg: OrgMembership | null;
  setActiveOrg: (slug: string) => void;
  isPlatformAdmin: boolean;
  signInDemo: () => Promise<void>;
  signInDev: () => Promise<void>;
  signOut: () => void;
  reloadSession: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [snapshot, setSnapshot] = useState<AuthSnapshot>({
    status: "loading",
    me: null,
    error: null,
  });
  const [activeOrgSlug, setActiveOrgSlug] = useState<string | null>(null);
  const mounted = useRef(true);
  const isMock = isMockMode();
  const isDevLogin = !isMock && isDevLoginEnabled();

  const load = useCallback(async (silent = false) => {
    if (!silent) {
      setSnapshot((current) =>
        current.status === "authenticated" ? current : { status: "loading", me: null, error: null },
      );
    }
    try {
      const me = await api.session.me();
      if (!mounted.current) return;
      setSnapshot({ status: "authenticated", me, error: null });
    } catch (error) {
      if (!mounted.current) return;
      if (isApiError(error) && error.code === "unauthenticated") {
        setSnapshot({ status: "anonymous", me: null, error });
      } else if (isApiError(error) && error.code === "tenant_suspended") {
        setSnapshot({ status: "suspended", me: null, error });
      } else {
        setSnapshot({ status: "error", me: null, error });
      }
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    void load();
    const unsubscribe = subscribeSessionLost((reason) => {
      if (!mounted.current) return;
      if (reason === "unauthenticated") {
        queryClient.clear();
        setSnapshot({ status: "anonymous", me: null, error: null });
      } else {
        // Fetch /me once more so the suspended screen can quote a safe trace ID.
        setSnapshot({ status: "suspended", me: null, error: null });
        void load(true);
      }
    });
    return () => {
      mounted.current = false;
      unsubscribe();
    };
  }, [load, queryClient]);

  const orgs = useMemo(() => snapshot.me?.orgs ?? [], [snapshot.me]);

  useEffect(() => {
    if (orgs.length === 0) {
      if (activeOrgSlug !== null) setActiveOrgSlug(null);
      return;
    }
    if (!activeOrgSlug || !orgs.some((org) => org.slug === activeOrgSlug)) {
      setActiveOrgSlug(orgs[0]?.slug ?? null);
    }
  }, [activeOrgSlug, orgs]);

  const activeOrg = useMemo(
    () => orgs.find((org) => org.slug === activeOrgSlug) ?? orgs[0] ?? null,
    [activeOrgSlug, orgs],
  );

  const setActiveOrg = useCallback(
    (slug: string) => {
      if (orgs.some((org) => org.slug === slug)) setActiveOrgSlug(slug);
    },
    [orgs],
  );

  const signInDemo = useCallback(async () => {
    if (!isMock) return;
    const { mockControls } = await import("../api/mocks/state");
    mockControls.signIn();
    await load();
  }, [isMock, load]);

  const signInDev = useCallback(async () => {
    if (!isDevLogin) return;
    const me = await api.session.devLogin();
    if (!mounted.current) return;
    queryClient.clear();
    setSnapshot({ status: "authenticated", me, error: null });
  }, [isDevLogin, queryClient]);

  const signOut = useCallback(() => {
    // Set the UI state synchronously so navigation to /login cannot bounce back
    // to a protected route while a dynamic mock import is in flight.
    queryClient.clear();
    setSnapshot({ status: "anonymous", me: null, error: null });
    if (isMock) {
      void import("../api/mocks/state").then(({ mockControls }) => mockControls.signOut());
    } else if (isDevLogin) {
      void api.session.logout().catch(() => undefined);
    }
    // A production SSO edge owns logout; the local API revokes the cookie when
    // developer login is enabled.
  }, [isDevLogin, isMock, queryClient]);

  const value = useMemo<AuthContextValue>(
    () => ({
      ...snapshot,
      isMock,
      isDevLogin,
      canSignOut: isMock || isDevLogin,
      orgs,
      activeOrg,
      setActiveOrg,
      isPlatformAdmin: snapshot.me?.is_platform_admin === true,
      signInDemo,
      signInDev,
      signOut,
      reloadSession: () => load(),
    }),
    [activeOrg, isDevLogin, isMock, load, orgs, setActiveOrg, signInDemo, signInDev, signOut, snapshot],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside AuthProvider");
  return value;
}
