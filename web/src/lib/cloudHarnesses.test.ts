import { describe, expect, it } from "vitest";

import { runsInCloudSandbox } from "./cloudHarnesses";

describe("runsInCloudSandbox", () => {
  it("accepts the included and own-key harnesses and nothing else", () => {
    expect(runsInCloudSandbox("claude-native")).toBe(true);
    expect(runsInCloudSandbox("codex-native")).toBe(true);
    expect(runsInCloudSandbox("cursor-native")).toBe(false);
    expect(runsInCloudSandbox("pi-native")).toBe(false);
    expect(runsInCloudSandbox(null)).toBe(false);
    expect(runsInCloudSandbox(undefined)).toBe(false);
  });
});
