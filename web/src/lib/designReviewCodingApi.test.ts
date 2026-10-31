import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  approveDesignSpec,
  listDesignApprovals,
  openDesignPullRequest,
} from "./designReviewCodingApi";
import { authenticatedFetch } from "./identity";

vi.mock("./identity", () => ({
  authenticatedFetch: vi.fn(),
}));

const mockAuthenticatedFetch = vi.mocked(authenticatedFetch);
const SHA256 = "a".repeat(64);
const COMMIT = "c".repeat(40);

const APPROVAL_WIRE = {
  id: "0123456789abcdef",
  spec_path: "run/r2.md",
  spec_sha256: SHA256,
  commit: COMMIT,
  coding_branch: "wakevane/design-0123456789",
  coding_session_id: "conv-2",
  coding_error: null,
  pull_request: null,
  coding_outputs: {
    checkpoint: 2,
    published_at: 1_700_000_000,
    files: [{ path: "a.py" }, { path: "b.py" }],
    deleted: [],
    tests: { sha256: SHA256, size: 10 },
  },
};

function okJson(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as unknown as Response;
}

describe("design review coding API", () => {
  beforeEach(() => mockAuthenticatedFetch.mockReset());

  it("approves a saved spec by path, hash and commit", async () => {
    mockAuthenticatedFetch.mockResolvedValue(okJson({ approval: APPROVAL_WIRE, created: true }));

    const approval = await approveDesignSpec("conv/1", {
      path: "run/r2.md",
      sha256: SHA256,
      commit: COMMIT,
    });

    expect(approval.codingSessionId).toBe("conv-2");
    expect(approval.codingOutputs).toEqual({
      checkpoint: 2,
      publishedAt: 1_700_000_000,
      changedFiles: 2,
      deletedFiles: 0,
      hasTestOutput: true,
    });
    const [path, init] = mockAuthenticatedFetch.mock.calls[0];
    expect(path).toBe("/v1/sessions/conv%2F1/design-review/approve");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual({
      path: "run/r2.md",
      sha256: SHA256,
      commit: COMMIT,
    });
  });

  it("lists approvals and opens a pull request for one", async () => {
    mockAuthenticatedFetch.mockResolvedValueOnce(
      okJson({ approvals: [{ ...APPROVAL_WIRE, coding_outputs: null }] }),
    );
    const [listed] = await listDesignApprovals("conv-1");
    expect(listed.codingOutputs).toBeNull();

    const pull = { number: 5, url: "https://github.com/o/r/pull/5", branch: "b", base: "main" };
    mockAuthenticatedFetch.mockResolvedValueOnce(
      okJson({ approval: { ...APPROVAL_WIRE, pull_request: pull } }),
    );
    const opened = await openDesignPullRequest("conv-1", listed.id);
    expect(opened.pullRequest).toEqual(pull);
    expect(mockAuthenticatedFetch.mock.calls[1][0]).toBe(
      "/v1/sessions/conv-1/design-review/approvals/0123456789abcdef/pull-request",
    );
  });

  it("surfaces server rejections", async () => {
    mockAuthenticatedFetch.mockResolvedValue({
      ok: false,
      status: 409,
      statusText: "Conflict",
      json: async () => ({ message: "The specification changed" }),
      text: async () => JSON.stringify({ message: "The specification changed" }),
      headers: new Headers({ "content-type": "application/json" }),
    } as unknown as Response);

    await expect(
      approveDesignSpec("conv-1", { path: "run/r2.md", sha256: SHA256, commit: null }),
    ).rejects.toThrow();
  });
});
