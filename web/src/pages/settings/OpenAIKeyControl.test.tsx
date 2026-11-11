import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  fetchOpenAIKeyStatus: vi.fn(),
  saveOpenAIKey: vi.fn(),
  saveOpenAIKeyModels: vi.fn(),
  deleteOpenAIKey: vi.fn(),
}));

vi.mock("@/lib/providerKeysApi", () => api);

import { OpenAIKeyControl } from "./OpenAIKeyControl";

const NONE = { connected: false, key_hint: null, models: [], updated_at: null };
const SAVED = { connected: true, key_hint: "9f2c", models: [], updated_at: 1 };
const KEY = "sk-test-0123456789abcdefghij";

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe("OpenAIKeyControl", () => {
  it("renders nothing when the server does not offer provider keys", async () => {
    api.fetchOpenAIKeyStatus.mockResolvedValue(null);
    const { container } = render(<OpenAIKeyControl />);

    await waitFor(() => expect(api.fetchOpenAIKeyStatus).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("saves a pasted key, clears the field and shows only the key's last characters", async () => {
    const user = userEvent.setup();
    api.fetchOpenAIKeyStatus.mockResolvedValue(NONE);
    api.saveOpenAIKey.mockResolvedValue(SAVED);
    render(<OpenAIKeyControl />);

    const field = await screen.findByLabelText("OpenAI API key");
    expect(field).toHaveAttribute("type", "password");
    expect(screen.getByTestId("openai-key-save")).toBeDisabled();
    await user.type(field, KEY);
    await user.click(screen.getByTestId("openai-key-save"));

    expect(api.saveOpenAIKey).toHaveBeenCalledWith(KEY, []);
    expect(await screen.findByText(/Key ending in 9f2c is saved/)).toBeInTheDocument();
    expect(screen.getByLabelText("Replacement OpenAI API key")).toHaveValue("");
    expect(screen.queryByText(new RegExp(KEY))).toBeNull();
  });

  it("saves a key together with the model it should run", async () => {
    const user = userEvent.setup();
    api.fetchOpenAIKeyStatus.mockResolvedValue(NONE);
    api.saveOpenAIKey.mockResolvedValue({ ...SAVED, models: ["model-a"] });
    render(<OpenAIKeyControl />);

    await user.type(await screen.findByLabelText("OpenAI API key"), KEY);
    await user.type(screen.getByLabelText("Codex model"), " model-a ");
    await user.click(screen.getByTestId("openai-key-save"));

    expect(api.saveOpenAIKey).toHaveBeenCalledWith(KEY, ["model-a"]);
    await waitFor(() => expect(screen.getByLabelText("Codex model")).toHaveValue("model-a"));
  });

  it("changes the model of a saved key without asking for the key again", async () => {
    const user = userEvent.setup();
    api.fetchOpenAIKeyStatus.mockResolvedValue({ ...SAVED, models: ["model-a"] });
    api.saveOpenAIKeyModels.mockResolvedValue({ ...SAVED, models: ["model-b"] });
    render(<OpenAIKeyControl />);

    const field = await screen.findByLabelText("Codex model");
    expect(field).toHaveValue("model-a");
    const save = screen.getByTestId("openai-key-save");
    expect(save).toBeDisabled();
    await user.clear(field);
    await user.type(field, "model-b");
    expect(save).toHaveTextContent("Save model");
    await user.click(save);

    expect(api.saveOpenAIKeyModels).toHaveBeenCalledWith(["model-b"]);
    expect(api.saveOpenAIKey).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByLabelText("Codex model")).toHaveValue("model-b"));
  });

  it("removes a saved key", async () => {
    const user = userEvent.setup();
    api.fetchOpenAIKeyStatus.mockResolvedValue(SAVED);
    api.deleteOpenAIKey.mockResolvedValue(NONE);
    render(<OpenAIKeyControl />);

    await user.click(await screen.findByTestId("openai-key-remove"));

    expect(api.deleteOpenAIKey).toHaveBeenCalledTimes(1);
    expect(await screen.findByLabelText("OpenAI API key")).toBeInTheDocument();
    expect(screen.queryByTestId("openai-key-remove")).toBeNull();
  });

  it("shows the server's reason when a key is rejected and keeps the draft", async () => {
    const user = userEvent.setup();
    api.fetchOpenAIKeyStatus.mockResolvedValue(NONE);
    api.saveOpenAIKey.mockRejectedValue(new Error("The key must be 20 to 512 characters"));
    render(<OpenAIKeyControl />);

    await user.type(await screen.findByLabelText("OpenAI API key"), "short");
    await user.click(screen.getByTestId("openai-key-save"));

    expect(await screen.findByRole("alert")).toHaveTextContent("The key must be 20 to 512");
    expect(screen.getByLabelText("OpenAI API key")).toHaveValue("short");
  });
});
