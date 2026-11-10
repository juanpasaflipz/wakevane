/**
 * Asks for a password reset link (accounts provider with mail configured).
 *
 * The server answers the same way whether or not an account matched, so the
 * confirmation here never says a link was definitely sent.
 */

import { useEffect, useState, type FormEvent } from "react";
import { requestPasswordReset } from "@/lib/accountsApi";
import { withBasePath } from "@/lib/basePath";
import { AuthPageFrame } from "@/pages/AuthPageFrame";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

export function ForgotPasswordPage() {
  const [identifier, setIdentifier] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [requested, setRequested] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const el = document.getElementById("forgot-identifier");
    if (el instanceof HTMLInputElement) el.focus();
  }, []);

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setError(null);
    const result = await requestPasswordReset(identifier.trim());
    setSubmitting(false);
    if (result.ok) {
      setRequested(true);
      return;
    }
    setError(result.error);
  }

  return (
    <AuthPageFrame>
      <div className="space-y-1 text-center">
        <h1 className="text-2xl font-semibold tracking-tight">Reset your password</h1>
        <p className="text-ui text-muted-foreground">
          Enter your username or the email on your account.
        </p>
      </div>

      {requested ? (
        <div
          role="status"
          className="rounded-md border border-border bg-muted px-3 py-2 text-ui text-foreground"
        >
          If an account matches, a reset link is on its way to the email on file. The link expires
          in one hour.
        </div>
      ) : (
        <form onSubmit={onSubmit} className="space-y-4">
          <div className="space-y-1.5">
            <label htmlFor="forgot-identifier" className="text-ui font-medium leading-none">
              Username or email
            </label>
            <Input
              id="forgot-identifier"
              type="text"
              autoComplete="username"
              value={identifier}
              onChange={(e) => setIdentifier(e.target.value)}
              disabled={submitting}
              required
              maxLength={254}
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
            disabled={submitting || identifier.trim().length === 0}
            componentId="forgot_password.send_link"
          >
            {submitting ? "Sending…" : "Send reset link"}
          </Button>
        </form>
      )}

      <p className="text-center text-sm text-muted-foreground">
        <a className="underline underline-offset-4" href={withBasePath("/login")}>
          Back to sign in
        </a>
      </p>
    </AuthPageFrame>
  );
}
