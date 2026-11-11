import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));

import { CONNECTED_REPOS, routeFetch } from "@/test/subscriptionFetchMocks";
import {
  REPOSITORY_FILTER_THRESHOLD,
  RepositoryPicker,
  filterRepositories,
} from "./RepositoryPicker";

function repo(fullName: string) {
  return {
    full_name: fullName,
    clone_url: null,
    default_branch: "main",
    private: false,
    pushed_at: null,
  };
}

function renderPicker(value = "") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <RepositoryPicker id="repo" value={value} onChange={vi.fn()} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function optionLabels(): string[] {
  return within(screen.getByLabelText("Repository"))
    .getAllByRole("option")
    .map((option) => option.textContent ?? "");
}

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe("filterRepositories", () => {
  const repos = [repo("acme/api"), repo("acme/web"), repo("Other/API-docs")];

  it("matches owner/name case-insensitively and keeps the selected repository", () => {
    expect(filterRepositories(repos, " api ", "").map((r) => r.full_name)).toEqual([
      "acme/api",
      "Other/API-docs",
    ]);
    expect(filterRepositories(repos, "docs", "acme/web").map((r) => r.full_name)).toEqual([
      "acme/web",
      "Other/API-docs",
    ]);
    expect(filterRepositories(repos, "", "")).toBe(repos);
  });
});

describe("RepositoryPicker", () => {
  it("shows no filter box for a short list", async () => {
    routeFetch({ "GET /v1/connections/github/repos": CONNECTED_REPOS });
    renderPicker();

    await screen.findByLabelText("Repository");
    expect(screen.queryByLabelText("Filter repositories")).toBeNull();
  });

  it("narrows a long list as the filter is typed", async () => {
    const user = userEvent.setup();
    const names = Array.from(
      { length: REPOSITORY_FILTER_THRESHOLD + 1 },
      (_, index) => `acme/service-${index}`,
    );
    routeFetch({
      "GET /v1/connections/github/repos": {
        body: {
          connected: true,
          truncated: false,
          repos: [...names, "acme/green-monkey"].map(repo),
        },
      },
    });
    renderPicker();

    await user.type(await screen.findByLabelText("Filter repositories"), "monkey");
    expect(optionLabels()).toEqual(["Choose a repository…", "acme/green-monkey"]);

    await user.clear(screen.getByLabelText("Filter repositories"));
    await user.type(screen.getByLabelText("Filter repositories"), "zzz");
    expect(optionLabels()).toEqual(["No repositories match"]);
  });
});
