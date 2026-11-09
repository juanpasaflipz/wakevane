import { authenticatedFetch } from "./identity";
import { withBasePath } from "./basePath";
import { apiErrorFromResponse } from "./sessionsApi";

export interface CostRange {
  low_usd: number;
  high_usd: number;
}

export interface ModelPlan {
  method: "rules" | "typesafe_assisted";
  repository: {
    name: string;
    commit: string;
    files_sampled: number;
    languages: string[];
    partial: boolean;
    test_files_detected: boolean;
  };
  configuration_revision: string;
  complexity: "standard" | "high";
  estimate: CostRange;
  steps: { role: "implement" | "review"; model_id: string; reason: string; estimate: CostRange }[];
  candidates: { id: string; label: string; estimate: CostRange }[];
  assumptions: {
    input_tokens_high: number;
    output_tokens_high: number;
    review_share: number;
    markup_percent: number;
  };
  escalation: string;
  warnings: string[];
}

export interface ModelPlanInput {
  repository: string;
  branch?: string | null;
  prompt: string;
  mode: "economy" | "balanced" | "quality";
  budget_usd: number;
  configuration_revision: string;
  agent_id?: string;
  analyze_with_ai: boolean;
}

export async function recommendModelPlan(
  provider: string,
  input: ModelPlanInput,
  signal?: AbortSignal,
): Promise<ModelPlan> {
  const response = await authenticatedFetch(
    withBasePath(
      `/v1/sandbox-providers/${encodeURIComponent(provider)}/harnesses/pi-native/model-plan`,
    ),
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
      signal,
    },
  );
  if (!response.ok) throw await apiErrorFromResponse(response);
  return (await response.json()) as ModelPlan;
}
