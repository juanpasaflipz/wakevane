import { authenticatedFetch } from "./identity";
import { apiErrorFromResponse } from "./sessionsApi";

/** One saved review file, as recorded in the durable manifest. */
export interface DesignReviewOutputFile {
  path: string;
  size: number;
  sha256: string;
}

/** The latest saved checkpoint of a cloud Design review's outputs. */
export interface DesignReviewOutputs {
  repository: string | null;
  commit: string | null;
  checkpoint: number;
  publishedAt: number;
  reason: string;
  files: DesignReviewOutputFile[];
}

interface DesignReviewOutputsWire {
  repository: string | null;
  commit: string | null;
  checkpoint: number;
  published_at: number;
  reason: string;
  files: DesignReviewOutputFile[];
}

function outputsPath(sessionId: string): string {
  return `/v1/sessions/${encodeURIComponent(sessionId)}/design-review/outputs`;
}

/** Saved outputs for a session's review tree, or `null` before the first checkpoint. */
export async function getDesignReviewOutputs(
  sessionId: string,
): Promise<DesignReviewOutputs | null> {
  const res = await authenticatedFetch(outputsPath(sessionId));
  if (res.status === 404) return null;
  if (!res.ok) throw await apiErrorFromResponse(res);
  const wire = (await res.json()) as DesignReviewOutputsWire;
  return {
    repository: wire.repository,
    commit: wire.commit,
    checkpoint: wire.checkpoint,
    publishedAt: wire.published_at,
    reason: wire.reason,
    files: wire.files,
  };
}

/** Download one saved file, verifying its bytes against the manifest hash. */
export async function downloadDesignReviewOutput(
  sessionId: string,
  file: DesignReviewOutputFile,
): Promise<Blob> {
  const encodedPath = file.path.split("/").map(encodeURIComponent).join("/");
  const res = await authenticatedFetch(`${outputsPath(sessionId)}/files/${encodedPath}`);
  if (!res.ok) throw await apiErrorFromResponse(res);
  const bytes = await res.arrayBuffer();
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  const hex = Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
  if (hex !== file.sha256) throw new Error(`${file.path} did not match its saved hash`);
  return new Blob([bytes], { type: "text/plain" });
}
