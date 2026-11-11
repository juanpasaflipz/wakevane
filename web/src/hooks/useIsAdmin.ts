import { useEffect, useSyncExternalStore } from "react";
import { getCurrentIsAdmin, resolveIdentity, subscribeIdentity } from "@/lib/identity";

// Mode-agnostic admin gate, sourced from the `/v1/me` identity probe
// (the shared `users.is_admin` column). Unlike `useMe`, which reads the
// accounts-only `/auth/me` endpoint, this works in EVERY auth mode —
// header, accounts, AND OIDC/SSO — so admin chrome (the Members /
// Policies settings sections) can surface under OIDC where `/auth/me`
// doesn't exist.

/**
 * Whether the current user is an admin, per `GET /v1/me`. Returns false
 * until identity resolves, then updates as soon as the probe settles —
 * including on a direct load of a settings URL, where the nav mounts
 * before the probe returns. Server enforces the flag on every admin
 * route regardless — this is chrome only.
 */
export function useIsAdmin(): boolean {
  const isAdmin = useSyncExternalStore(subscribeIdentity, getCurrentIsAdmin, () => false);
  useEffect(() => {
    void resolveIdentity();
  }, []);
  return isAdmin;
}
