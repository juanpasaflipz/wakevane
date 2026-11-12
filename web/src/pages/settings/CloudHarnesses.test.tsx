import { cleanup, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ fetchOpenAIKeyStatus: vi.fn() }));
vi.mock("@/lib/providerKeysApi", () => api);

import { CloudHarnesses, type CloudHarnessEntry } from "./CloudHarnesses";

const ENTRIES: CloudHarnessEntry[] = ["claude-native", "codex-native", "pi-native"].map(
  (harness) => ({ harness, name: harness, description: `about ${harness}`, icon: null }),
);

function renderPage() {
  render(
    <MemoryRouter>
      <CloudHarnesses entries={ENTRIES} />
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe("CloudHarnesses", () => {
  it("shows the included agent as ready and offers the key step for the own-key agent", async () => {
    api.fetchOpenAIKeyStatus.mockResolvedValue({
      connected: false,
      key_hint: null,
      models: [],
      updated_at: null,
    });
    renderPage();

    const included = screen.getByTestId("cloud-harness-claude-native");
    expect(within(included).getByText(/Included with your trial credit/)).toBeInTheDocument();
    const ownKey = await screen.findByTestId("cloud-harness-codex-native");
    expect(within(ownKey).getByRole("link", { name: "Add your key" })).toHaveAttribute(
      "href",
      "/settings/integrations",
    );
    expect(screen.getByTestId("cloud-harnesses-unavailable")).toHaveTextContent("pi-native.");
  });

  it("reports the own-key agent ready with the saved key and model", async () => {
    api.fetchOpenAIKeyStatus.mockResolvedValue({
      connected: true,
      key_hint: "9f2c",
      models: ["model-a"],
      updated_at: 1,
    });
    renderPage();

    const ownKey = await screen.findByTestId("cloud-harness-codex-native");
    expect(ownKey).toHaveTextContent("key ending in 9f2c, model model-a");
    expect(within(ownKey).queryByRole("link")).toBeNull();
  });

  it("lists the own-key agent as unavailable when the deployment does not offer member keys", async () => {
    api.fetchOpenAIKeyStatus.mockResolvedValue(null);
    renderPage();

    await vi.waitFor(() => expect(api.fetchOpenAIKeyStatus).toHaveBeenCalled());
    expect(screen.queryByTestId("cloud-harness-codex-native")).toBeNull();
    expect(screen.getByTestId("cloud-harnesses-unavailable")).toHaveTextContent(
      "codex-native, pi-native.",
    );
  });
});
