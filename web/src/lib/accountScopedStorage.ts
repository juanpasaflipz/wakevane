// Launch preferences kept in this browser belong to whoever was signed in when
// they were written. When a different account signs in on the same browser,
// drop them so the newcomer does not inherit the previous member's choices.

const LAST_ACCOUNT_KEY = "omnigent:last-account";

/** Browser-stored launch preferences that describe one member's own work. */
export const ACCOUNT_SCOPED_KEYS = [
  "omnigent:last-sandbox-repos",
  "omnigent:last-sandbox-repo",
  "omnigent:recent-workspaces",
] as const;

/**
 * Record the signed-in account, clearing account-scoped preferences when it
 * differs from the account this browser last saw. A signed-out probe
 * (``null``) changes nothing. Never throws.
 */
export function noteSignedInAccount(userId: string | null): void {
  if (userId === null || typeof window === "undefined") return;
  try {
    const previous = window.localStorage.getItem(LAST_ACCOUNT_KEY);
    if (previous === userId) return;
    if (previous !== null) {
      for (const key of ACCOUNT_SCOPED_KEYS) window.localStorage.removeItem(key);
    }
    window.localStorage.setItem(LAST_ACCOUNT_KEY, userId);
  } catch {
    // Storage may be unavailable; preferences are a convenience only.
  }
}
