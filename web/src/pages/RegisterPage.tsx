/**
 * The invite-redemption page (``/register?invite=...``).
 *
 * Reached by clicking the copyable URL an admin minted in the
 * Members page. The user chooses their own username + password,
 * the server consumes the invite + creates the account + sets the
 * session cookie, and we navigate to ``/``.
 *
 * Mounted outside the AppShell for the same reason LoginPage is —
 * the chrome loads sidebar / conversations / runner hooks that
 * require an authenticated identity.
 *
 * Username constraints are intentionally restrictive to match the
 * server's validation regex
 * (``^[a-z0-9][a-z0-9._-]{0,63}(@[a-z0-9.-]+\.[a-z]{2,})?$``).
 * The form lowercases on input so the user can't accidentally
 * type a mixed-case value that the server then rejects.
 */

import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { useSearchParams } from "@/lib/routing";
import { useLoginV2 } from "@/lib/useLoginV2";
import { AuthCardShell } from "@/pages/onboarding/AuthCardShell";
import { JoinTeamStep } from "@/pages/onboarding/JoinTeamStep";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { register as registerRequest } from "@/lib/accountsApi";
import { withBasePath } from "@/lib/basePath";
import { useServerInfo } from "@/lib/CapabilitiesContext";

const MIN_PASSWORD_LENGTH = 8;

export function RegisterPage() {
  const loginV2 = useLoginV2();
  const info = useServerInfo();
  const [params] = useSearchParams();
  const invite = params.get("invite") ?? "";
  // A public deploy registers without an invite; the server still refuses an
  // invite-less POST when open signup is off.
  const openSignup = info !== "loading" && info.open_signup === true;
  const termsUrl = info !== "loading" ? (info.terms_url ?? null) : null;
  const privacyUrl = info !== "loading" ? (info.privacy_url ?? null) : null;

  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [accepted, setAccepted] = useState(false); // v2: invite landing accepted

  // Server rejects invites that are missing / expired / redeemed
  // with a generic 400. Surface the same generic UI in the
  // most-common bad case (no invite param at all).
  const missingInvite = invite === "" && !openSignup;

  useEffect(() => {
    const el = document.getElementById("register-username");
    if (el instanceof HTMLInputElement) el.focus();
  }, []);

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (submitting) return;
    setError(null);

    if (password !== confirm) {
      setError("Passwords don't match.");
      return;
    }
    if (password.length < MIN_PASSWORD_LENGTH) {
      setError(`Password must be at least ${MIN_PASSWORD_LENGTH} characters.`);
      return;
    }

    setSubmitting(true);
    const trimmedEmail = email.trim();
    const result = await registerRequest(
      invite === ""
        ? { username, password, ...(trimmedEmail === "" ? {} : { email: trimmedEmail }) }
        : { invite, username, password },
    );
    if (result.ok) {
      // A pending verification lands on Billing, where the credit and the
      // resend button live; everyone else goes to the home page.
      window.location.href = withBasePath(
        result.email_verification ? "/settings/billing?verify=sent" : "/",
      );
      return;
    }
    setSubmitting(false);
    setError(result.error);
  }

  const body: ReactNode = (
    <>
      <div className="space-y-1 text-center">
        <h1 className="text-2xl font-semibold tracking-tight">Create your account</h1>
        <p className="text-ui text-muted-foreground">
          {invite === ""
            ? "Sign up to run design reviews in the cloud. Pick a plan after you sign in."
            : "You were invited to join this server."}
        </p>
      </div>

      {missingInvite ? (
        <div
          role="alert"
          className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-ui text-destructive"
        >
          This page needs an invite token in the URL — make sure you opened the link your admin sent
          you.
        </div>
      ) : (
        <form onSubmit={onSubmit} className="space-y-4">
          <div className="space-y-1.5">
            <label htmlFor="register-username" className="text-ui font-medium leading-none">
              Username
            </label>
            <Input
              id="register-username"
              type="text"
              autoComplete="username"
              value={username}
              onChange={(e) => setUsername(e.target.value.toLowerCase())}
              disabled={submitting}
              required
              pattern="[a-z0-9][a-z0-9._\-]{0,63}(@[a-z0-9.\-]+\.[a-z]{2,})?"
              title="Lowercase letters, digits, dots, hyphens, underscores (or a lowercase email)"
            />
            <p className="text-sm text-muted-foreground">
              Lowercase letters, digits, dots, hyphens, underscores — or a lowercase email.
            </p>
          </div>

          {invite === "" && (
            <div className="space-y-1.5">
              <label htmlFor="register-email" className="text-ui font-medium leading-none">
                Email
              </label>
              <Input
                id="register-email"
                type="email"
                autoComplete="email"
                inputMode="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                disabled={submitting}
                required
                maxLength={254}
              />
              <p className="text-sm text-muted-foreground">
                We send one link to confirm it and unlock your trial credit. Not used to sign in.
              </p>
            </div>
          )}

          <div className="space-y-1.5">
            <label htmlFor="register-password" className="text-ui font-medium leading-none">
              Password
            </label>
            <Input
              id="register-password"
              type="password"
              autoComplete="new-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              disabled={submitting}
              required
              minLength={MIN_PASSWORD_LENGTH}
            />
          </div>

          <div className="space-y-1.5">
            <label htmlFor="register-confirm" className="text-ui font-medium leading-none">
              Confirm password
            </label>
            <Input
              id="register-confirm"
              type="password"
              autoComplete="new-password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              disabled={submitting}
              required
              minLength={MIN_PASSWORD_LENGTH}
            />
          </div>

          {error !== null && (
            <div
              role="alert"
              className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-ui text-destructive"
            >
              {error}
            </div>
          )}

          <Button
            type="submit"
            className="w-full"
            disabled={
              submitting ||
              password.length < MIN_PASSWORD_LENGTH ||
              username.length === 0 ||
              (invite === "" && email.trim().length === 0)
            }
            componentId="register.create_account"
          >
            {submitting ? "Creating…" : "Create account"}
          </Button>

          {(termsUrl !== null || privacyUrl !== null) && (
            <p className="text-center text-sm text-muted-foreground">
              By creating an account you agree to the{" "}
              {termsUrl !== null && (
                <a
                  className="underline underline-offset-4"
                  href={termsUrl}
                  target="_blank"
                  rel="noreferrer"
                >
                  Terms of Service
                </a>
              )}
              {termsUrl !== null && privacyUrl !== null && " and "}
              {privacyUrl !== null && (
                <a
                  className="underline underline-offset-4"
                  href={privacyUrl}
                  target="_blank"
                  rel="noreferrer"
                >
                  Privacy Policy
                </a>
              )}
              .
            </p>
          )}
        </form>
      )}

      {openSignup && invite === "" && (
        <p className="text-center text-sm text-muted-foreground">
          Already have an account?{" "}
          <a className="underline underline-offset-4" href={withBasePath("/login")}>
            Sign in
          </a>
        </p>
      )}
    </>
  );

  // v2: a valid invite shows the "Join your team" landing first (Accept →
  // form); a missing invite skips it. One shell across both steps so the panel
  // isn't remounted and its animation restarts between steps.
  if (loginV2) {
    const showLanding = !missingInvite && !accepted;
    return (
      <AuthCardShell panelHeight={showLanding ? 340 : undefined}>
        {showLanding ? (
          <JoinTeamStep onAccept={() => setAccepted(true)} serverUrl={window.location.host} />
        ) : (
          <div className="flex flex-col gap-6">{body}</div>
        )}
      </AuthCardShell>
    );
  }

  return (
    <div
      className="flex min-h-screen items-center justify-center bg-background px-4"
      // Centered auth page (no header / native bars): just keep the card clear
      // of the notch + home indicator. 0 off the iOS shell. See index.css.
      style={{
        paddingTop: "var(--omnigent-safe-top)",
        paddingBottom: "var(--omnigent-safe-bottom)",
      }}
    >
      <div className="w-full max-w-sm space-y-6">{body}</div>
    </div>
  );
}
