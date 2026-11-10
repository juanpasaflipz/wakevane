// Tests for ForgotPasswordPage (/forgot-password): the request goes to
// requestPasswordReset() and the confirmation never claims an account exists.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ForgotPasswordPage } from "./ForgotPasswordPage";
import * as accountsApi from "@/lib/accountsApi";

vi.mock("@/lib/accountsApi", () => ({ requestPasswordReset: vi.fn() }));

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/forgot-password"]}>
      <ForgotPasswordPage />
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  window.localStorage.clear();
});

describe("ForgotPasswordPage", () => {
  it("sends the trimmed identifier and shows a neutral confirmation", async () => {
    vi.mocked(accountsApi.requestPasswordReset).mockResolvedValue({ ok: true });
    renderPage();

    fireEvent.change(screen.getByLabelText(/username or email/i), {
      target: { value: "  ana@example.com " },
    });
    fireEvent.click(screen.getByRole("button", { name: /send reset link/i }));

    await waitFor(() => expect(screen.getByRole("status")).toBeTruthy());
    expect(accountsApi.requestPasswordReset).toHaveBeenCalledWith("ana@example.com");
    expect(screen.getByRole("status").textContent).toMatch(/if an account matches/i);
    expect(screen.queryByRole("button", { name: /send reset link/i })).toBeNull();
  });

  it("shows the server's error and keeps the form", async () => {
    vi.mocked(accountsApi.requestPasswordReset).mockResolvedValue({
      ok: false,
      error: "Too many attempts. Wait a while and try again.",
    });
    renderPage();

    fireEvent.change(screen.getByLabelText(/username or email/i), { target: { value: "ana" } });
    fireEvent.click(screen.getByRole("button", { name: /send reset link/i }));

    await waitFor(() => expect(screen.getByRole("alert").textContent).toMatch(/too many/i));
    expect(screen.getByRole("button", { name: /send reset link/i })).toBeTruthy();
  });

  it("links back to sign in", () => {
    renderPage();
    expect(screen.getByRole("link", { name: /back to sign in/i }).getAttribute("href")).toBe(
      "/login",
    );
  });
});
