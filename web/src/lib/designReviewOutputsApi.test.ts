import { beforeEach, describe, expect, it, vi } from "vitest";
import { downloadDesignReviewOutput, getDesignReviewOutputs } from "./designReviewOutputsApi";
import { authenticatedFetch } from "./identity";

vi.mock("./identity", () => ({
  authenticatedFetch: vi.fn(),
}));

const mockAuthenticatedFetch = vi.mocked(authenticatedFetch);
const SPEC = new TextEncoder().encode("# Spec\n");
const WRONG_SHA256 = "0".repeat(64);

async function sha256Hex(bytes: Uint8Array<ArrayBuffer>): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

describe("design review outputs API", () => {
  beforeEach(() => mockAuthenticatedFetch.mockReset());

  it("reports no saved outputs before the first checkpoint", async () => {
    mockAuthenticatedFetch.mockResolvedValue({ ok: false, status: 404 } as Response);

    await expect(getDesignReviewOutputs("conv/1")).resolves.toBeNull();
    expect(mockAuthenticatedFetch).toHaveBeenCalledWith(
      "/v1/sessions/conv%2F1/design-review/outputs",
    );
  });

  it("downloads only bytes that match the saved hash", async () => {
    const file = { path: "run 1/spec.md", size: SPEC.length, sha256: await sha256Hex(SPEC) };
    mockAuthenticatedFetch.mockResolvedValue({
      ok: true,
      status: 200,
      arrayBuffer: async () => SPEC.buffer.slice(0),
    } as unknown as Response);

    const blob = await downloadDesignReviewOutput("conv-1", file);

    expect(await blob.text()).toBe("# Spec\n");
    expect(mockAuthenticatedFetch).toHaveBeenCalledWith(
      "/v1/sessions/conv-1/design-review/outputs/files/run%201/spec.md",
    );
    await expect(
      downloadDesignReviewOutput("conv-1", { ...file, sha256: WRONG_SHA256 }),
    ).rejects.toThrow("did not match its saved hash");
  });
});
