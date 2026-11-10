// Tests for ResetPasswordPage (/reset-password?t=…): token gating, client-side
// password checks, and the signed-out success state.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ResetPasswordPage } from "./ResetPasswordPage";
import * as accountsApi from "@/lib/accountsApi";

vi.mock("@/lib/accountsApi", () => ({ resetPassword: vi.fn() }));

function renderAt(search: string) {
  return render(
    <MemoryRouter initialEntries={[`/reset-password${search}`]}>
      <ResetPasswordPage />
    </MemoryRouter>,
  );
}

function fill(password: string, confirm: string) {
  fireEvent.change(screen.getByLabelText(/^new password/i), { target: { value: password } });
  fireEvent.change(screen.getByLabelText(/confirm new password/i), {
    target: { value: confirm },
  });
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  window.localStorage.clear();
});

describe("ResetPasswordPage", () => {
  it("asks for a new link when the token is missing", () => {
    renderAt("");
    expect(screen.getByRole("alert").textContent).toMatch(/link from your reset email/i);
    expect(screen.queryByLabelText(/^new password/i)).toBeNull();
    expect(screen.getByRole("link", { name: /request a new link/i }).getAttribute("href")).toBe(
      "/forgot-password",
    );
  });

  it("rejects mismatched passwords without calling the server", () => {
    renderAt("?t=tok");
    fill("new-password-2", "different-password");
    fireEvent.click(screen.getByRole("button", { name: /save new password/i }));
    expect(screen.getByRole("alert").textContent).toMatch(/don't match/i);
    expect(accountsApi.resetPassword).not.toHaveBeenCalled();
  });

  it("submits the token and password, then points at sign in", async () => {
    vi.mocked(accountsApi.resetPassword).mockResolvedValue({ ok: true });
    renderAt("?t=tok.en-1");
    fill("new-password-2", "new-password-2");
    fireEvent.click(screen.getByRole("button", { name: /save new password/i }));

    await waitFor(() => expect(screen.getByRole("status")).toBeTruthy());
    expect(accountsApi.resetPassword).toHaveBeenCalledWith("tok.en-1", "new-password-2");
    expect(screen.getByRole("link", { name: /sign in/i }).getAttribute("href")).toBe("/login");
  });

  it("offers a new link when the server says the link expired", async () => {
    vi.mocked(accountsApi.resetPassword).mockResolvedValue({
      ok: false,
      error: "reset link invalid or expired",
    });
    renderAt("?t=old");
    fill("new-password-2", "new-password-2");
    fireEvent.click(screen.getByRole("button", { name: /save new password/i }));

    await waitFor(() => expect(screen.getByRole("alert").textContent).toMatch(/expired/i));
    expect(screen.getByRole("link", { name: /request a new link/i })).toBeTruthy();
  });
});
