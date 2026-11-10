/**
 * Layout for the signed-out password pages: the v2 card shell when the
 * login-v2 flag is on, otherwise the same centered column as LoginPage.
 */

import type { ReactNode } from "react";
import { useLoginV2 } from "@/lib/useLoginV2";
import { AuthCardShell } from "@/pages/onboarding/AuthCardShell";

export function AuthPageFrame({ children }: { children: ReactNode }) {
  const loginV2 = useLoginV2();
  if (loginV2) {
    return (
      <AuthCardShell>
        <div className="flex flex-col gap-6">{children}</div>
      </AuthCardShell>
    );
  }
  return (
    <div
      className="flex min-h-screen items-center justify-center bg-background px-4"
      // Keep the card clear of the notch + home indicator. See index.css.
      style={{
        paddingTop: "var(--omnigent-safe-top)",
        paddingBottom: "var(--omnigent-safe-bottom)",
      }}
    >
      <div className="w-full max-w-sm space-y-6">{children}</div>
    </div>
  );
}
