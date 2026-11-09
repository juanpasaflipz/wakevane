import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ModelPlanPanel } from "./ModelPlanPanel";
import { recommendModelPlan, type ModelPlan } from "@/lib/modelPlansApi";

vi.mock("@/lib/modelPlansApi", () => ({ recommendModelPlan: vi.fn() }));
afterEach(cleanup);
beforeEach(() => vi.resetAllMocks());

const plan: ModelPlan = {
  method: "rules",
  configuration_revision: "r1",
  complexity: "standard",
  repository: {
    name: "example/demo",
    commit: "a".repeat(40),
    files_sampled: 10,
    partial: false,
    languages: ["Python"],
    test_files_detected: true,
  },
  estimate: { low_usd: 0.25, high_usd: 1 },
  steps: [
    {
      role: "implement",
      model_id: "main",
      reason: "Deployment default",
      estimate: { low_usd: 0.25, high_usd: 1 },
    },
    {
      role: "review",
      model_id: "cheap",
      reason: "Independent review",
      estimate: { low_usd: 0.01, high_usd: 0.04 },
    },
  ],
  candidates: [
    { id: "main", label: "Main", estimate: { low_usd: 0.25, high_usd: 1 } },
    { id: "cheap", label: "Cheap", estimate: { low_usd: 0.025, high_usd: 0.1 } },
  ],
  assumptions: {
    input_tokens_high: 30000,
    output_tokens_high: 5000,
    review_share: 0.35,
    markup_percent: 20,
  },
  warnings: ["Review and escalation are manual."],
  escalation: "Review failures before spending more.",
};
const props = {
  provider: "test",
  repository: "https://github.com/example/demo",
  branch: "main",
  prompt: "Synthetic task",
  revision: "r1",
  agentId: "agent",
  selectedModel: "",
  onApply: vi.fn(),
};

function openPanel() {
  fireEvent.click(screen.getByText("Plan model usage"));
}

it("requires a click, defaults AI consent off, supports overrides and applies only the primary model", async () => {
  vi.mocked(recommendModelPlan).mockResolvedValue(plan);
  render(<ModelPlanPanel {...props} />);
  expect(recommendModelPlan).not.toHaveBeenCalled();
  openPanel();
  fireEvent.click(screen.getByRole("button", { name: "Recommend models" }));
  expect(await screen.findByText(/Model estimate: \$0.26–\$1.03/)).toBeInTheDocument();
  expect(recommendModelPlan).toHaveBeenCalledWith(
    "test",
    expect.objectContaining({
      analyze_with_ai: false,
      configuration_revision: "r1",
      agent_id: "agent",
      prompt: "Synthetic task",
    }),
    expect.any(AbortSignal),
  );
  expect(props.onApply).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText("Implementation model"), { target: { value: "cheap" } });
  expect(screen.getByText(/Model estimate: \$0.03–\$0.14/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Use implementation model" }));
  expect(props.onApply).toHaveBeenCalledWith("cheap");
  expect(screen.getByText(/Review and escalation do not run automatically/)).toBeInTheDocument();
});

it("invalidates the plan on preference changes and sends explicit AI consent", async () => {
  vi.mocked(recommendModelPlan).mockResolvedValue(plan);
  render(<ModelPlanPanel {...props} />);
  openPanel();
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "Recommend models" }));
  await screen.findByRole("button", { name: "Use implementation model" });
  expect(recommendModelPlan).toHaveBeenCalledWith(
    "test",
    expect.objectContaining({ analyze_with_ai: true }),
    expect.any(AbortSignal),
  );
  fireEvent.change(screen.getByLabelText("Planning preference"), { target: { value: "economy" } });
  expect(screen.queryByRole("button", { name: "Use implementation model" })).toBeNull();
  fireEvent.change(screen.getByLabelText("Planning budget"), { target: { value: "0" } });
  expect(screen.getByRole("button", { name: "Recommend models" })).toBeDisabled();
});

it("aborts stale requests when launch inputs change and ignores late results", async () => {
  let complete!: (result: ModelPlan) => void;
  vi.mocked(recommendModelPlan).mockImplementation(
    () =>
      new Promise((resolve) => {
        complete = resolve;
      }),
  );
  const view = render(<ModelPlanPanel key="old" {...props} />);
  openPanel();
  fireEvent.click(screen.getByRole("button", { name: "Recommend models" }));
  const signal = vi.mocked(recommendModelPlan).mock.calls[0][2]!;
  view.rerender(<ModelPlanPanel key="new" {...props} prompt="New task" />);
  expect(signal.aborted).toBe(true);
  await act(async () => complete(plan));
  expect(screen.queryByText(/Model estimate:/)).toBeNull();
});

it("shows actionable backend errors without selecting a model", async () => {
  vi.mocked(recommendModelPlan).mockRejectedValue(new Error("Connect your GitHub account"));
  render(<ModelPlanPanel {...props} />);
  openPanel();
  fireEvent.click(screen.getByRole("button", { name: "Recommend models" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Connect your GitHub account");
  expect(props.onApply).not.toHaveBeenCalled();
});
