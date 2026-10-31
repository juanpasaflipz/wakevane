import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DesignReviewOutputs } from "./DesignReviewOutputs";
import {
  approveDesignSpec,
  listDesignApprovals,
  openDesignPullRequest,
} from "@/lib/designReviewCodingApi";
import { getDesignReviewOutputs } from "@/lib/designReviewOutputsApi";

vi.mock("@/lib/designReviewOutputsApi", () => ({
  getDesignReviewOutputs: vi.fn(),
  downloadDesignReviewOutput: vi.fn(),
}));
vi.mock("@/lib/designReviewCodingApi", () => ({
  approveDesignSpec: vi.fn(),
  listDesignApprovals: vi.fn(),
  openDesignPullRequest: vi.fn(),
}));

const SHA256 = "a".repeat(64);
const COMMIT = "c".repeat(40);

function renderOutputs(props: { codingEnabled: boolean; isOwner: boolean }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <DesignReviewOutputs sessionId="conv-1" {...props} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function expand() {
  const trigger = await screen.findByText(/2 saved files/);
  fireEvent.click(trigger);
}

describe("DesignReviewOutputs approval", () => {
  beforeEach(() => {
    vi.mocked(getDesignReviewOutputs).mockResolvedValue({
      repository: "https://github.com/org/repo.git",
      commit: COMMIT,
      checkpoint: 1,
      publishedAt: 1_700_000_000,
      reason: "checkpoint",
      files: [
        { path: "run/r2.md", size: 10, sha256: SHA256 },
        { path: "run/notes.txt", size: 3, sha256: "b".repeat(64) },
      ],
    });
    vi.mocked(listDesignApprovals).mockResolvedValue([]);
  });
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("lets the owner approve a saved Markdown spec by its hash and commit", async () => {
    vi.mocked(approveDesignSpec).mockResolvedValue({} as never);
    renderOutputs({ codingEnabled: true, isOwner: true });
    await expand();

    const approve = await screen.findAllByRole("button", { name: /Approve for coding/ });
    expect(approve).toHaveLength(1);
    fireEvent.click(approve[0]);

    await waitFor(() =>
      expect(approveDesignSpec).toHaveBeenCalledWith("conv-1", {
        path: "run/r2.md",
        sha256: SHA256,
        commit: COMMIT,
      }),
    );
  });

  it("hides approval from non-owners and when the flag is off", async () => {
    renderOutputs({ codingEnabled: true, isOwner: false });
    await expand();
    expect(screen.queryByRole("button", { name: /Approve for coding/ })).toBeNull();
    cleanup();
    vi.mocked(listDesignApprovals).mockClear();

    renderOutputs({ codingEnabled: false, isOwner: true });
    await expand();
    expect(screen.queryByRole("button", { name: /Approve for coding/ })).toBeNull();
    expect(listDesignApprovals).not.toHaveBeenCalled();
  });

  it("links the coding session and opens a pull request on request", async () => {
    vi.mocked(listDesignApprovals).mockResolvedValue([
      {
        id: "0123456789abcdef",
        specPath: "run/r2.md",
        specSha256: SHA256,
        commit: COMMIT,
        codingBranch: "wakevane/design-0123456789",
        codingSessionId: "conv-2",
        codingError: null,
        pullRequest: null,
        codingOutputs: {
          checkpoint: 1,
          publishedAt: 1_700_000_000,
          changedFiles: 3,
          deletedFiles: 0,
          hasTestOutput: true,
        },
      },
    ]);
    vi.mocked(openDesignPullRequest).mockResolvedValue({} as never);
    renderOutputs({ codingEnabled: true, isOwner: true });
    await expand();

    const link = await screen.findByRole("link", { name: "Coding session" });
    expect(link.getAttribute("href")).toBe("/c/conv-2");
    expect(screen.getByText(/3 changed, 0 deleted · test output saved/)).toBeTruthy();
    // The approved revision is not offered for approval again.
    expect(screen.queryByRole("button", { name: /Approve for coding/ })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /Open pull request/ }));
    await waitFor(() =>
      expect(openDesignPullRequest).toHaveBeenCalledWith("conv-1", "0123456789abcdef"),
    );
  });
});
