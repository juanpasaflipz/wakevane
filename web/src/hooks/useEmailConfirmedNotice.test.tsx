import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

const toast = vi.hoisted(() => ({ success: vi.fn(), message: vi.fn() }));
vi.mock("sonner", () => ({ toast }));

import {
  EMAIL_CONFIRMED_NO_CREDIT,
  EMAIL_CONFIRMED_WITH_CREDIT,
  useEmailConfirmedNotice,
} from "./useEmailConfirmedNotice";

function Probe() {
  useEmailConfirmedNotice();
  const location = useLocation();
  return <div data-testid="where">{location.pathname + location.search}</div>;
}

function renderAt(entry: string) {
  render(
    <MemoryRouter initialEntries={[entry]}>
      <Probe />
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe("useEmailConfirmedNotice", () => {
  it("announces the trial credit once and clears the marker, keeping other parameters", async () => {
    renderAt("/?verified=1&panel=agents");

    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/?panel=agents"));
    expect(toast.success).toHaveBeenCalledTimes(1);
    expect(toast.success).toHaveBeenCalledWith(EMAIL_CONFIRMED_WITH_CREDIT);
  });

  it("says so when the mailbox already had its credit", async () => {
    renderAt("/?verified=1&credit=0");

    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent(/^\/$/));
    expect(toast.message).toHaveBeenCalledWith(EMAIL_CONFIRMED_NO_CREDIT);
    expect(toast.success).not.toHaveBeenCalled();
  });

  it("stays quiet without the marker and leaves billing's own results alone", () => {
    renderAt("/c/abc");
    renderAt("/settings/billing?verified=1");

    expect(toast.success).not.toHaveBeenCalled();
    expect(toast.message).not.toHaveBeenCalled();
  });
});
