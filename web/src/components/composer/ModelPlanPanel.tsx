import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { recommendModelPlan, type ModelPlan, type ModelPlanInput } from "@/lib/modelPlansApi";

interface Props {
  provider: string;
  repository: string;
  branch?: string | null;
  prompt: string;
  revision: string;
  agentId?: string;
  selectedModel: string;
  onApply: (model: string) => void;
}

const dollars = (amount: number) => `$${amount.toFixed(amount > 0 && amount < 0.01 ? 4 : 2)}`;

/** The parent keys this panel by launch inputs, so edits discard stale recommendations. */
export function ModelPlanPanel(props: Props) {
  const [mode, setMode] = useState<ModelPlanInput["mode"]>("balanced");
  const [budget, setBudget] = useState("2");
  const [analyzeWithAi, setAnalyzeWithAi] = useState(false);
  const [plan, setPlan] = useState<ModelPlan | null>(null);
  const [models, setModels] = useState<string[]>([]);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => controller.current?.abort(), []);
  const validBudget =
    Number.isFinite(Number(budget)) && Number(budget) > 0 && Number(budget) <= 1000;

  async function recommend() {
    controller.current?.abort();
    const request = new AbortController();
    controller.current = request;
    setPending(true);
    setError(null);
    setPlan(null);
    try {
      const result = await recommendModelPlan(
        props.provider,
        {
          repository: props.repository,
          branch: props.branch,
          prompt: props.prompt,
          configuration_revision: props.revision,
          agent_id: props.agentId,
          mode,
          budget_usd: Number(budget),
          analyze_with_ai: analyzeWithAi,
        },
        request.signal,
      );
      if (request.signal.aborted) return;
      setPlan(result);
      setModels(result.steps.map((step) => step.model_id));
    } catch (cause) {
      if (!request.signal.aborted)
        setError(cause instanceof Error ? cause.message : "Could not recommend models");
    } finally {
      if (!request.signal.aborted) setPending(false);
    }
  }

  const total = { low_usd: 0, high_usd: 0 };
  plan?.steps.forEach((step, index) => {
    const candidate = plan.candidates.find((row) => row.id === models[index]);
    if (!candidate) return;
    const share = step.role === "review" ? plan.assumptions.review_share : 1;
    total.low_usd += candidate.estimate.low_usd * share;
    total.high_usd += candidate.estimate.high_usd * share;
  });

  return (
    <details
      className="mb-3 rounded-lg border border-border p-3 text-sm"
      data-testid="model-plan-panel"
    >
      <summary className="cursor-pointer font-medium">Plan model usage</summary>
      <p className="my-2 text-muted-foreground">
        Inspect this repository and suggest models before starting.
      </p>
      <div className="flex flex-wrap items-end gap-3">
        <label>
          Preference
          <select
            aria-label="Planning preference"
            value={mode}
            disabled={pending}
            className="ml-2 rounded border bg-background p-1"
            onChange={(event) => {
              setMode(event.target.value as ModelPlanInput["mode"]);
              setPlan(null);
            }}
          >
            <option value="economy">Economy</option>
            <option value="balanced">Balanced</option>
            <option value="quality">Quality (extra review)</option>
          </select>
        </label>
        <label>
          Planning budget ($)
          <input
            aria-label="Planning budget"
            className="ml-2 w-20 rounded border bg-background p-1"
            type="number"
            min="0.01"
            max="1000"
            step="0.01"
            value={budget}
            disabled={pending}
            onChange={(event) => {
              setBudget(event.target.value);
              setPlan(null);
            }}
          />
        </label>
        <Button
          type="button"
          variant="outline"
          disabled={pending || !validBudget || !props.prompt.trim()}
          onClick={() => void recommend()}
        >
          {pending ? "Inspecting repository…" : "Recommend models"}
        </Button>
      </div>
      <p className="mt-2 text-xs text-muted-foreground">
        A planning target, not a spending cap. Account credit and overage limits still apply.
      </p>
      <label className="my-3 flex items-start gap-2 text-xs">
        <input
          type="checkbox"
          checked={analyzeWithAi}
          disabled={pending}
          onChange={(event) => {
            setAnalyzeWithAi(event.target.checked);
            setPlan(null);
          }}
        />
        Use AI task analysis: send my prompt and aggregate repository statistics to TypeSafe. Source
        files and file names are not sent. Requires server configuration.
      </label>
      {error && (
        <p role="alert" className="mt-2 text-destructive">
          {error}
        </p>
      )}
      {plan && (
        <div className="mt-3 space-y-3" aria-live="polite">
          <p>
            {plan.repository.name} · commit {plan.repository.commit.slice(0, 8)} ·{" "}
            {plan.repository.files_sampled} files sampled
            {plan.repository.partial ? " (partial)" : ""} ·{" "}
            {plan.repository.languages.join(", ") || "languages undetermined"}
          </p>
          <p>
            {plan.method === "typesafe_assisted"
              ? "AI-assisted task assessment"
              : "Rules-based task assessment"}{" "}
            · {plan.complexity} scope
          </p>
          {plan.steps.map((step, index) => (
            <div key={step.role}>
              <label className="font-medium">
                {step.role === "implement" ? "Implementation model" : "Suggested reviewer (manual)"}
                <select
                  aria-label={
                    step.role === "implement" ? "Implementation model" : "Suggested reviewer"
                  }
                  value={models[index]}
                  className="ml-2 max-w-full rounded border bg-background p-1"
                  onChange={(event) =>
                    setModels((previous) =>
                      previous.map((value, i) => (i === index ? event.target.value : value)),
                    )
                  }
                >
                  {plan.candidates.map((row) => (
                    <option key={row.id} value={row.id}>
                      {row.label}
                    </option>
                  ))}
                </select>
              </label>
              <p className="text-xs text-muted-foreground">
                {models[index] === step.model_id
                  ? step.reason
                  : "Your model override; estimate updated below."}
              </p>
            </div>
          ))}
          <p className="font-medium">
            Model estimate: {dollars(total.low_usd)}–{dollars(total.high_usd)}
            {total.high_usd > Number(budget) && " · exceeds planning budget"}
          </p>
          <p className="text-xs text-muted-foreground">
            Assumes up to {plan.assumptions.input_tokens_high.toLocaleString()} input and{" "}
            {plan.assumptions.output_tokens_high.toLocaleString()} output tokens for implementation;
            review adds {Math.round(plan.assumptions.review_share * 100)}% of that usage. Includes{" "}
            {plan.assumptions.markup_percent}% configured service markup. Compute and retries are
            additional.
          </p>
          <details>
            <summary className="cursor-pointer">Estimate limits and next steps</summary>
            <ul className="list-disc space-y-1 pl-5 text-xs text-muted-foreground">
              {plan.warnings
                .filter((warning) => !warning.startsWith("The upper estimate"))
                .map((warning) => (
                  <li key={warning}>{warning}</li>
                ))}
            </ul>
            <p className="mt-2 text-xs">{plan.escalation}</p>
            <p className="mt-2 text-xs">
              The repository may change after inspection. This recommendation does not pin the
              session checkout.
            </p>
          </details>
          <Button
            type="button"
            variant="outline"
            disabled={!models[0]}
            onClick={() => props.onApply(models[0])}
          >
            Use implementation model
          </Button>
          {props.selectedModel === models[0] && (
            <span className="ml-2 text-xs">Selected for launch</span>
          )}
          <p className="text-xs text-muted-foreground">
            Review and escalation do not run automatically. Start the chat when ready.
          </p>
        </div>
      )}
    </details>
  );
}
