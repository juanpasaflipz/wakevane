/**
 * Sets a new password from an emailed reset link (``/reset-password?t=…``).
 *
 * A successful reset signs the account out everywhere, so the page sends the
 * user to the sign-in form afterwards instead of signing them in.
 */

import { useEffect, useState, type FormEvent } from "react";
import { resetPassword } from "@/lib/accountsApi";
import { withBasePath } from "@/lib/basePath";
import { useSearchParams } from "@/lib/routing";
import { AuthPageFrame } from "@/pages/AuthPageFrame";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

const MIN_PASSWORD_LENGTH = 8;

export function ResetPasswordPage() {
  const [params] = useSearchParams();
  const token = params.get("t") ?? "";

  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const el = document.getElementById("reset-password");
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
    const result = await resetPassword(token, password);
    setSubmitting(false);
    if (result.ok) {
      setDone(true);
      return;
    }
    setError(result.error);
  }

  const heading = (
    <div className="space-y-1 text-center">
      <h1 className="text-2xl font-semibold tracking-tight">Choose a new password</h1>
    </div>
  );

  if (token === "") {
    return (
      <AuthPageFrame>
        {heading}
        <div
          role="alert"
          className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-ui text-destructive"
        >
          This page needs the link from your reset email. Request a new one if it is missing.
        </div>
        <p className="text-center text-sm text-muted-foreground">
          <a className="underline underline-offset-4" href={withBasePath("/forgot-password")}>
            Request a new link
          </a>
        </p>
      </AuthPageFrame>
    );
  }

  if (done) {
    return (
      <AuthPageFrame>
        {heading}
        <div
          role="status"
          className="rounded-md border border-border bg-muted px-3 py-2 text-ui text-foreground"
        >
          Your password was changed and every other session was signed out.
        </div>
        <Button asChild className="w-full" componentId="reset_password.go_to_sign_in">
          <a href={withBasePath("/login")}>Sign in</a>
        </Button>
      </AuthPageFrame>
    );
  }

  return (
    <AuthPageFrame>
      {heading}
      <form onSubmit={onSubmit} className="space-y-4">
        <div className="space-y-1.5">
          <label htmlFor="reset-password" className="text-ui font-medium leading-none">
            New password
          </label>
          <Input
            id="reset-password"
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
          <label htmlFor="reset-confirm" className="text-ui font-medium leading-none">
            Confirm new password
          </label>
          <Input
            id="reset-confirm"
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
            {error.includes("invalid or expired") && (
              <>
                {" "}
                <a className="underline underline-offset-4" href={withBasePath("/forgot-password")}>
                  Request a new link
                </a>
                .
              </>
            )}
          </div>
        )}

        <Button
          type="submit"
          className="w-full"
          disabled={submitting || password.length < MIN_PASSWORD_LENGTH}
          componentId="reset_password.save"
        >
          {submitting ? "Saving…" : "Save new password"}
        </Button>
      </form>
    </AuthPageFrame>
  );
}
