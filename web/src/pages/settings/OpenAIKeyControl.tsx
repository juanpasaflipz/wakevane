// Save, replace or remove the user's own OpenAI API key. With a key saved,
// sessions the user creates afterwards can run the Codex harnesses in their
// managed sandboxes. The key is write-only here: the server returns only its
// last characters.

import { useCallback, useEffect, useState } from "react";
import { SettingsLabel } from "@/components/SettingsLabel";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  deleteOpenAIKey,
  fetchOpenAIKeyStatus,
  saveOpenAIKey,
  saveOpenAIKeyModels,
  type OpenAIKeyStatus,
} from "@/lib/providerKeysApi";

const CONNECTED_HELP =
  "New sessions can run Codex in your sandboxes on this key. OpenAI bills you directly for that usage.";
const UNCONNECTED_HELP =
  "Add your own OpenAI API key so new sessions can run Codex in your sandboxes. OpenAI bills you directly for that usage.";

const MODEL_HELP =
  "Leave the model empty for the default. Your OpenAI organization must offer the model you name.";

/** The single model the card edits; a key saved with several keeps its first. */
function savedModel(status: OpenAIKeyStatus): string {
  return status.models[0] ?? "";
}

export function OpenAIKeyControl() {
  const [status, setStatus] = useState<OpenAIKeyStatus | null | "loading">("loading");
  const [draft, setDraft] = useState("");
  const [model, setModel] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        const loaded = await fetchOpenAIKeyStatus();
        setStatus(loaded);
        if (loaded) setModel(savedModel(loaded));
      } catch {
        setStatus(null);
      }
    })();
  }, []);

  const run = useCallback(async (action: () => Promise<OpenAIKeyStatus>) => {
    setBusy(true);
    setError(null);
    try {
      const next = await action();
      setStatus(next);
      setModel(savedModel(next));
      setDraft("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Something went wrong.");
    } finally {
      setBusy(false);
    }
  }, []);

  // Nothing to show while loading, or when the server does not offer provider keys.
  if (status === "loading" || status === null) return null;

  const models = model.trim() === "" ? [] : [model.trim()];
  const modelChanged = model.trim() !== savedModel(status);
  const modelOnly = status.connected && draft.trim() === "";

  return (
    <div className="flex flex-col gap-3" data-testid="openai-key-control">
      <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3">
        <SettingsLabel
          label="OpenAI API key"
          labelClassName="text-sm"
          className="flex-1"
          description={
            status.connected
              ? `Key ending in ${status.key_hint} is saved. ${CONNECTED_HELP} ${MODEL_HELP}`
              : `${UNCONNECTED_HELP} ${MODEL_HELP}`
          }
        />
        {status.connected && (
          <Button
            variant="ghost"
            size="sm"
            className="h-9 shrink-0"
            disabled={busy}
            data-testid="openai-key-remove"
            onClick={() => void run(deleteOpenAIKey)}
          >
            Remove
          </Button>
        )}
      </div>
      <form
        className="flex flex-wrap items-center gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          void run(() => (modelOnly ? saveOpenAIKeyModels(models) : saveOpenAIKey(draft, models)));
        }}
      >
        <Input
          type="password"
          autoComplete="off"
          aria-label={status.connected ? "Replacement OpenAI API key" : "OpenAI API key"}
          placeholder={status.connected ? "Paste a new key to replace it" : "Paste your key"}
          className="h-9 min-w-56 flex-1"
          value={draft}
          disabled={busy}
          onChange={(event) => setDraft(event.target.value)}
        />
        <Input
          type="text"
          autoComplete="off"
          spellCheck={false}
          aria-label="Codex model"
          placeholder="Codex model (default)"
          className="h-9 min-w-44 flex-1"
          value={model}
          disabled={busy}
          onChange={(event) => setModel(event.target.value)}
        />
        <Button
          type="submit"
          size="sm"
          className="h-9"
          disabled={busy || (modelOnly ? !modelChanged : draft.trim() === "")}
          data-testid="openai-key-save"
        >
          {modelOnly ? "Save model" : status.connected ? "Replace key" : "Save key"}
        </Button>
      </form>
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
    </div>
  );
}
