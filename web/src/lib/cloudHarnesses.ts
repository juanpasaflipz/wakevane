// Which coding agents a hosted, sandbox-only deployment can run.

/** Runs in a sandbox on the operator's own model access. */
export const CLOUD_INCLUDED_HARNESSES: ReadonlySet<string> = new Set(["claude-native"]);
/** Runs in a sandbox on a key the member supplies. */
export const CLOUD_OWN_KEY_HARNESSES: ReadonlySet<string> = new Set(["codex-native"]);

/** Whether *harness* can run in a hosted sandbox at all. */
export function runsInCloudSandbox(harness: string | null | undefined): boolean {
  return (
    harness != null &&
    (CLOUD_INCLUDED_HARNESSES.has(harness) || CLOUD_OWN_KEY_HARNESSES.has(harness))
  );
}

/** Why a key-backed harness is off until the member saves their own key. */
export const OWN_KEY_REQUIRED_MESSAGE =
  "Codex runs on your own OpenAI key. Add one in Settings → Sandbox Integrations.";
