import { authenticatedFetch } from "./identity";
import { apiErrorFromResponse } from "./sessionsApi";

/** Pull request opened from an approval's stored coding changes. */
export interface DesignPullRequest {
  number: number;
  url: string;
  branch: string;
  base: string;
}

/** Latest stored changes of an approval's coding session. */
export interface DesignCodingOutputs {
  checkpoint: number;
  publishedAt: number;
  changedFiles: number;
  deletedFiles: number;
  hasTestOutput: boolean;
}

/** An owner's approval of one saved specification for implementation. */
export interface DesignApproval {
  id: string;
  specPath: string;
  specSha256: string;
  commit: string;
  codingBranch: string;
  codingSessionId: string | null;
  codingError: string | null;
  pullRequest: DesignPullRequest | null;
  codingOutputs: DesignCodingOutputs | null;
}

interface DesignApprovalWire {
  id: string;
  spec_path: string;
  spec_sha256: string;
  commit: string;
  coding_branch: string;
  coding_session_id: string | null;
  coding_error: string | null;
  pull_request: DesignPullRequest | null;
  coding_outputs?: {
    checkpoint: number;
    published_at: number;
    files: unknown[];
    deleted: unknown[];
    tests: unknown;
  } | null;
}

function reviewPath(sessionId: string): string {
  return `/v1/sessions/${encodeURIComponent(sessionId)}/design-review`;
}

function fromWire(wire: DesignApprovalWire): DesignApproval {
  const outputs = wire.coding_outputs ?? null;
  return {
    id: wire.id,
    specPath: wire.spec_path,
    specSha256: wire.spec_sha256,
    commit: wire.commit,
    codingBranch: wire.coding_branch,
    codingSessionId: wire.coding_session_id,
    codingError: wire.coding_error,
    pullRequest: wire.pull_request,
    codingOutputs: outputs
      ? {
          checkpoint: outputs.checkpoint,
          publishedAt: outputs.published_at,
          changedFiles: outputs.files.length,
          deletedFiles: outputs.deleted.length,
          hasTestOutput: outputs.tests != null,
        }
      : null,
  };
}

async function postJson(path: string, body?: unknown): Promise<DesignApproval> {
  const res = await authenticatedFetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) throw await apiErrorFromResponse(res);
  const data = (await res.json()) as { approval: DesignApprovalWire };
  return fromWire(data.approval);
}

/** Approve a saved specification (by path and hash) and start its coding session. */
export function approveDesignSpec(
  sessionId: string,
  spec: { path: string; sha256: string; commit: string | null },
): Promise<DesignApproval> {
  return postJson(`${reviewPath(sessionId)}/approve`, {
    path: spec.path,
    sha256: spec.sha256,
    ...(spec.commit ? { commit: spec.commit } : {}),
  });
}

/** Approvals recorded for a review, newest last. */
export async function listDesignApprovals(sessionId: string): Promise<DesignApproval[]> {
  const res = await authenticatedFetch(`${reviewPath(sessionId)}/approvals`);
  if (!res.ok) throw await apiErrorFromResponse(res);
  const data = (await res.json()) as { approvals: DesignApprovalWire[] };
  return data.approvals.map(fromWire);
}

/** Open a pull request from an approval's stored changes. Never merges. */
export function openDesignPullRequest(
  sessionId: string,
  approvalId: string,
): Promise<DesignApproval> {
  return postJson(
    `${reviewPath(sessionId)}/approvals/${encodeURIComponent(approvalId)}/pull-request`,
  );
}
