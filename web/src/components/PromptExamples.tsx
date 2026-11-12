// Starter prompts under the landing composer. Picking one fills the composer so
// a newcomer can see what a good task looks like, then edit it before sending.

import { useState } from "react";
import { XIcon } from "lucide-react";

const HIDDEN_KEY = "omnigent:prompt-examples-hidden";

export interface PromptExample {
  /** Short card heading, e.g. "Fix a bug". */
  title: string;
  /** The text placed in the composer. */
  prompt: string;
  /** Skill the prompt starts with, when it needs one, e.g. "investigate". */
  skill?: string;
}

export const PROMPT_EXAMPLES: readonly PromptExample[] = [
  {
    title: "Understand a codebase",
    prompt:
      "Explain how this repository is organized and how to run it. Read-only: do not change anything.",
  },
  {
    title: "Fix a bug",
    prompt:
      "Fix the failing test in <file or test name>. Run the test suite, and open a pull request when it passes.",
  },
  {
    title: "Check production readiness",
    skill: "investigate",
    prompt:
      "/investigate Is this repository ready for production? Run lint, typecheck, tests and build, and report what fails. Read-only.",
  },
  {
    title: "Do several jobs at once",
    skill: "fanout",
    prompt:
      "/fanout Add unit tests for each file in <folder> that has none. One pull request per file.",
  },
  {
    title: "Get a second opinion",
    skill: "cross-review",
    prompt:
      "/cross-review Review pull request #<number> for bugs and security problems. Report only; do not change code.",
  },
];

function readHidden(): boolean {
  try {
    return window.localStorage.getItem(HIDDEN_KEY) === "1";
  } catch {
    return false;
  }
}

/**
 * Example cards for the landing composer.
 *
 * :param skills: Names of the skills the selected agent offers; an example
 *   that starts with a skill is shown only when that skill is available.
 * :param onPick: Called with the example's prompt text.
 */
export function PromptExamples({
  skills,
  onPick,
}: {
  skills: readonly string[];
  onPick: (prompt: string) => void;
}) {
  const [hidden, setHidden] = useState(readHidden);
  const examples = PROMPT_EXAMPLES.filter(
    (example) => example.skill === undefined || skills.includes(example.skill),
  );
  if (hidden || examples.length === 0) return null;

  const hide = () => {
    setHidden(true);
    try {
      window.localStorage.setItem(HIDDEN_KEY, "1");
    } catch {
      // Storage may be unavailable; the examples come back on the next visit.
    }
  };

  return (
    <section className="mt-6 w-full" aria-label="Example prompts" data-testid="prompt-examples">
      <div className="mb-2 flex items-center justify-between">
        <h2 className="text-ui text-muted-foreground">
          Try an example, then edit it to fit your repository
        </h2>
        <button
          type="button"
          onClick={hide}
          aria-label="Hide examples"
          data-testid="prompt-examples-hide"
          className="rounded-md p-1 text-muted-foreground transition-colors hover:text-foreground"
        >
          <XIcon className="size-4" aria-hidden />
        </button>
      </div>
      <ul className="grid gap-2 sm:grid-cols-2">
        {examples.map((example) => (
          <li key={example.title}>
            <button
              type="button"
              onClick={() => onPick(example.prompt)}
              data-testid="prompt-example"
              className="flex h-full w-full flex-col gap-1 rounded-lg border border-border p-3 text-left transition-colors hover:bg-muted/50"
            >
              <span className="text-sm font-medium">{example.title}</span>
              <span className="line-clamp-2 text-ui text-muted-foreground">{example.prompt}</span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
