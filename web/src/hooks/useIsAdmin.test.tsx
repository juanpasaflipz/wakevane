import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const identity = vi.hoisted(() => ({
  isAdmin: false,
  listeners: new Set<() => void>(),
  resolveIdentity: vi.fn(async () => "root"),
}));

vi.mock("@/lib/identity", () => ({
  getCurrentIsAdmin: () => identity.isAdmin,
  resolveIdentity: identity.resolveIdentity,
  subscribeIdentity: (listener: () => void) => {
    identity.listeners.add(listener);
    return () => identity.listeners.delete(listener);
  },
}));

import { useIsAdmin } from "./useIsAdmin";

describe("useIsAdmin", () => {
  beforeEach(() => {
    identity.isAdmin = false;
    identity.listeners.clear();
    identity.resolveIdentity.mockClear();
  });

  it("turns true when the identity probe settles after the first render", () => {
    // A direct load of a settings URL mounts the nav before /v1/me returns.
    const { result } = renderHook(() => useIsAdmin());
    expect(result.current).toBe(false);
    expect(identity.resolveIdentity).toHaveBeenCalledTimes(1);

    act(() => {
      identity.isAdmin = true;
      for (const listener of identity.listeners) listener();
    });
    expect(result.current).toBe(true);
  });

  it("is true on first render when identity already resolved", () => {
    identity.isAdmin = true;
    const { result } = renderHook(() => useIsAdmin());
    expect(result.current).toBe(true);
  });
});
