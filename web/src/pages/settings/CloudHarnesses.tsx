// Settings → Harnesses on a hosted, sandbox-only deployment. There is no host
// to inspect, so the page says which agents run in a cloud sandbox, what each
// one runs on, and how to turn on the ones that need the member's own key.

import { useEffect, useState, type ReactNode } from "react";
import { CheckCircle2Icon, KeyRoundIcon } from "lucide-react";
import { Link } from "@/lib/routing";
import { Button } from "@/components/ui/button";
import { fetchOpenAIKeyStatus, type OpenAIKeyStatus } from "@/lib/providerKeysApi";

export interface CloudHarnessEntry {
  harness: string;
  name: string;
  description: string;
  icon: ReactNode;
}

// Agents the operator's own model access runs in a sandbox.
const INCLUDED_HARNESSES = new Set(["claude-native"]);
// Agents that run in a sandbox on a key the member supplies.
const OWN_KEY_HARNESSES = new Set(["codex-native"]);

function Card({ entry, children }: { entry: CloudHarnessEntry; children: ReactNode }) {
  return (
    <li
      className="flex flex-col gap-3 rounded-lg border border-border p-4"
      data-testid={`cloud-harness-${entry.harness}`}
    >
      <div className="flex items-center gap-3">
        <div className="flex size-10 shrink-0 items-center justify-center rounded-lg border border-border [&_img]:size-5 [&_svg]:size-5">
          {entry.icon}
        </div>
        <h3 className="text-sm font-medium">{entry.name}</h3>
      </div>
      <p className="text-sm text-muted-foreground">{entry.description}</p>
      {children}
    </li>
  );
}

function Ready({ children }: { children: ReactNode }) {
  return (
    <p className="flex items-start gap-1.5 text-sm text-success">
      <CheckCircle2Icon className="mt-0.5 size-4 shrink-0" aria-hidden />
      <span>{children}</span>
    </p>
  );
}

function OwnKeyStatus({ keyStatus }: { keyStatus: OpenAIKeyStatus }) {
  if (keyStatus.connected) {
    const model = keyStatus.models[0];
    return (
      <Ready>
        Ready. Runs on your OpenAI key ending in {keyStatus.key_hint}
        {model ? `, model ${model}` : ""}. OpenAI bills you directly.
      </Ready>
    );
  }
  return (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <p className="flex items-start gap-1.5 text-sm text-muted-foreground">
        <KeyRoundIcon className="mt-0.5 size-4 shrink-0" aria-hidden />
        <span>Needs your own OpenAI API key. OpenAI bills you directly.</span>
      </p>
      <Button asChild size="sm" variant="outline" className="h-9">
        <Link to="/settings/integrations">Add your key</Link>
      </Button>
    </div>
  );
}

export function CloudHarnesses({ entries }: { entries: CloudHarnessEntry[] }) {
  // null: the deployment does not offer member keys, or the status is unknown.
  const [keyStatus, setKeyStatus] = useState<OpenAIKeyStatus | null>(null);
  useEffect(() => {
    let active = true;
    void fetchOpenAIKeyStatus()
      .then((status) => {
        if (active) setKeyStatus(status);
      })
      .catch(() => undefined);
    return () => {
      active = false;
    };
  }, []);

  const included = entries.filter((entry) => INCLUDED_HARNESSES.has(entry.harness));
  const ownKey = keyStatus ? entries.filter((entry) => OWN_KEY_HARNESSES.has(entry.harness)) : [];
  const available = new Set([...included, ...ownKey].map((entry) => entry.harness));
  const unavailable = entries.filter((entry) => !available.has(entry.harness));

  return (
    <div className="flex flex-col gap-8" data-testid="cloud-harnesses">
      <header className="space-y-1">
        <h2 className="text-xl font-semibold">Harnesses</h2>
        <p className="text-sm text-muted-foreground">
          The coding agents that can work in your cloud sandboxes.
        </p>
      </header>
      <section className="space-y-3">
        <h3 className="text-sm font-medium">Runs in your sandboxes</h3>
        <ul className="grid gap-3 @2xl:grid-cols-2">
          {included.map((entry) => (
            <Card key={entry.harness} entry={entry}>
              <Ready>Ready. Included with your trial credit or subscription.</Ready>
            </Card>
          ))}
          {keyStatus &&
            ownKey.map((entry) => (
              <Card key={entry.harness} entry={entry}>
                <OwnKeyStatus keyStatus={keyStatus} />
              </Card>
            ))}
        </ul>
      </section>
      {unavailable.length > 0 && (
        <section className="space-y-2" data-testid="cloud-harnesses-unavailable">
          <h3 className="text-sm font-medium">Not available in the cloud yet</h3>
          <p className="text-sm text-muted-foreground">
            {unavailable.map((entry) => entry.name).join(", ")}.
          </p>
        </section>
      )}
    </div>
  );
}
