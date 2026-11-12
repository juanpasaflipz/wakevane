// Tell a member their email was confirmed when the confirmation link brings
// them back into the app, then drop the marker from the address bar.

import { useEffect } from "react";
import { toast } from "sonner";
import { useLocation, useSearchParams } from "@/lib/routing";

export const EMAIL_CONFIRMED_WITH_CREDIT = "Email confirmed. Your trial credit is ready.";
export const EMAIL_CONFIRMED_NO_CREDIT =
  "Email confirmed. This mailbox already received a trial credit.";

export function useEmailConfirmedNotice(): void {
  const [searchParams, setSearchParams] = useSearchParams();
  const { pathname } = useLocation();
  const confirmed = searchParams.get("verified") === "1";
  const withoutCredit = searchParams.get("credit") === "0";
  // Settings › Billing announces its own confirmation results.
  const ownedByBilling = pathname.includes("/settings/");

  useEffect(() => {
    if (!confirmed || ownedByBilling) return;
    if (withoutCredit) toast.message(EMAIL_CONFIRMED_NO_CREDIT);
    else toast.success(EMAIL_CONFIRMED_WITH_CREDIT);
    setSearchParams(
      (current) => {
        const next = new URLSearchParams(current);
        next.delete("verified");
        next.delete("credit");
        return next;
      },
      { replace: true },
    );
  }, [confirmed, withoutCredit, ownedByBilling, setSearchParams]);
}
