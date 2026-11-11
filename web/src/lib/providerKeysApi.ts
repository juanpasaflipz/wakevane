/**
 * Client for the caller's own model-provider API key
 * (``/v1/provider-keys/openai``).
 *
 * A saved key lets the user's managed sandboxes run the Codex harnesses. The
 * server never returns the key, only its last characters.
 */

import { authenticatedFetch } from "./identity";

const OPENAI_KEY_PATH = "/v1/provider-keys/openai";

/** Shape of every ``/v1/provider-keys/openai`` response. */
export interface OpenAIKeyStatus {
  /** Whether the current user has saved a key. */
  connected: boolean;
  /** Last characters of the saved key, or null. */
  key_hint: string | null;
  /** Model ids the key is restricted to; empty for the default list. */
  models: string[];
  /** Unix epoch seconds the key was last saved, or null. */
  updated_at: number | null;
}

async function readStatus(res: Response, action: string): Promise<OpenAIKeyStatus> {
  if (!res.ok) {
    const body = (await res.json().catch(() => null)) as {
      error?: { message?: string };
    } | null;
    throw new Error(body?.error?.message ?? `${action} failed: ${res.status}`);
  }
  return (await res.json()) as OpenAIKeyStatus;
}

/**
 * Fetch the current user's key status. Resolves to ``null`` when the server
 * does not offer provider keys (feature off or no credential store).
 */
export async function fetchOpenAIKeyStatus(): Promise<OpenAIKeyStatus | null> {
  const res = await authenticatedFetch(OPENAI_KEY_PATH);
  if (res.status === 404) return null;
  return readStatus(res, "Key status");
}

/** Save or replace the current user's key, with the models it should run. */
export async function saveOpenAIKey(
  apiKey: string,
  models: string[] = [],
): Promise<OpenAIKeyStatus> {
  const res = await authenticatedFetch(OPENAI_KEY_PATH, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ api_key: apiKey, models }),
  });
  return readStatus(res, "Saving the key");
}

/** Change the models the saved key runs; an empty list means the default. */
export async function saveOpenAIKeyModels(models: string[]): Promise<OpenAIKeyStatus> {
  const res = await authenticatedFetch(OPENAI_KEY_PATH, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ models }),
  });
  return readStatus(res, "Saving the model");
}

/** Remove the current user's key. */
export async function deleteOpenAIKey(): Promise<OpenAIKeyStatus> {
  const res = await authenticatedFetch(OPENAI_KEY_PATH, { method: "DELETE" });
  return readStatus(res, "Removing the key");
}
