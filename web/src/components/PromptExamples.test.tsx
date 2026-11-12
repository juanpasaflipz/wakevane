import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PROMPT_EXAMPLES, PromptExamples } from "./PromptExamples";

beforeEach(() => window.localStorage.clear());
afterEach(cleanup);

describe("PromptExamples", () => {
  it("fills the composer with the picked example", async () => {
    const onPick = vi.fn();
    render(<PromptExamples skills={["investigate", "fanout", "cross-review"]} onPick={onPick} />);

    expect(screen.getAllByTestId("prompt-example")).toHaveLength(PROMPT_EXAMPLES.length);
    await userEvent.click(screen.getByRole("button", { name: /Fix a bug/ }));

    expect(onPick).toHaveBeenCalledWith(expect.stringContaining("Fix the failing test"));
  });

  it("leaves out examples that need a skill the selected agent does not offer", () => {
    render(<PromptExamples skills={["investigate"]} onPick={vi.fn()} />);

    expect(screen.getByRole("button", { name: /Check production readiness/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Do several jobs at once/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Get a second opinion/ })).toBeNull();
  });

  it("stays hidden once dismissed, including on the next visit", async () => {
    const first = render(<PromptExamples skills={[]} onPick={vi.fn()} />);
    await userEvent.click(screen.getByTestId("prompt-examples-hide"));
    expect(screen.queryByTestId("prompt-examples")).toBeNull();

    first.unmount();
    render(<PromptExamples skills={[]} onPick={vi.fn()} />);
    expect(screen.queryByTestId("prompt-examples")).toBeNull();
  });
});
