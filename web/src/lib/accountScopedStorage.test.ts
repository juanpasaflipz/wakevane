import { beforeEach, describe, expect, it } from "vitest";

import { noteSignedInAccount } from "./accountScopedStorage";

const REPOS = "omnigent:last-sandbox-repos";
const UNRELATED = "omnigent:theme";

beforeEach(() => {
  window.localStorage.clear();
  window.localStorage.setItem(
    REPOS,
    JSON.stringify([{ url: "https://example.test/r", branch: "" }]),
  );
  window.localStorage.setItem(UNRELATED, "dark");
});

describe("noteSignedInAccount", () => {
  it("keeps a returning member's preferences, including on the first visit after upgrade", () => {
    noteSignedInAccount("alice");
    noteSignedInAccount("alice");
    noteSignedInAccount(null);

    expect(window.localStorage.getItem(REPOS)).not.toBeNull();
  });

  it("drops the previous member's launch preferences when another account signs in", () => {
    noteSignedInAccount("alice");
    noteSignedInAccount("bob");

    expect(window.localStorage.getItem(REPOS)).toBeNull();
    expect(window.localStorage.getItem(UNRELATED)).toBe("dark");

    // Bob's own later choices survive his next visit.
    window.localStorage.setItem(REPOS, "[]");
    noteSignedInAccount("bob");
    expect(window.localStorage.getItem(REPOS)).toBe("[]");
  });
});
