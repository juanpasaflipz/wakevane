// Repository picker shared by the "Connect a repository" dialog and the
// "Start a software factory" card. Reads the caller's GitHub repositories and,
// when GitHub is not linked, points them at the Settings connect flow instead.

import { useState } from "react";
import { Loader2Icon } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/scheduled/Label";
import { useGithubRepos } from "@/hooks/useSubscriptions";
import { useAppName } from "@/lib/branding";
import { Link } from "@/lib/routing";

/** Where the existing "Connect GitHub" control lives. */
export const GITHUB_CONNECT_SETTINGS_PATH = "/settings/integrations";

export const NATIVE_SELECT_CLASS =
  "h-9 w-full rounded-md border border-input bg-background px-2 text-ui outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:opacity-50";

export function GithubNotConnectedNotice() {
  const appName = useAppName();
  return (
    <div
      role="status"
      data-testid="github-not-connected"
      className="rounded-md border border-border bg-muted/40 px-3 py-2 text-sm"
    >
      Connect your GitHub account first so {appName} can list your repositories and register the
      webhook for you.{" "}
      <Link
        to={GITHUB_CONNECT_SETTINGS_PATH}
        componentId="subscriptions.connect_github"
        className="text-primary underline"
      >
        Open Settings → Sandbox Integrations
      </Link>
    </div>
  );
}

// A list longer than this gets a filter box above the select.
export const REPOSITORY_FILTER_THRESHOLD = 10;

/** Repositories whose `owner/name` contains *query*, always keeping *selected*. */
export function filterRepositories<Repo extends { full_name: string }>(
  repos: Repo[],
  query: string,
  selected: string,
): Repo[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return repos;
  return repos.filter(
    (repo) => repo.full_name === selected || repo.full_name.toLowerCase().includes(needle),
  );
}

interface RepositoryPickerProps {
  id: string;
  value: string;
  onChange: (fullName: string) => void;
  /** Disables the select while a submission runs. */
  disabled?: boolean;
}

/**
 * A select of `owner/name` values; renders the not-connected or unavailable
 * notice in place of the select when there is nothing to pick from.
 */
export function RepositoryPicker({ id, value, onChange, disabled }: RepositoryPickerProps) {
  const { data, isLoading, isError } = useGithubRepos();
  const [query, setQuery] = useState("");

  if (isLoading) {
    return (
      <p className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2Icon className="size-3.5 animate-spin" />
        Loading repositories…
      </p>
    );
  }
  if (isError) {
    return (
      <p role="status" data-testid="github-unavailable" className="text-sm text-muted-foreground">
        GitHub is not set up on this server. An operator must configure the GitHub App first.
      </p>
    );
  }
  if (!data?.connected) return <GithubNotConnectedNotice />;

  const visibleRepos = filterRepositories(data.repos, query, value);

  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={id}>Repository</Label>
      {data.repos.length > REPOSITORY_FILTER_THRESHOLD && (
        <Input
          type="search"
          aria-label="Filter repositories"
          placeholder="Filter repositories…"
          data-testid={`${id}-filter`}
          value={query}
          disabled={disabled}
          onChange={(event) => setQuery(event.target.value)}
        />
      )}
      <select
        id={id}
        className={NATIVE_SELECT_CLASS}
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="">
          {query.trim() && visibleRepos.length === 0
            ? "No repositories match"
            : "Choose a repository…"}
        </option>
        {visibleRepos.map((repo) => (
          <option key={repo.full_name} value={repo.full_name}>
            {repo.full_name}
          </option>
        ))}
      </select>
      {data.truncated && (
        <p className="text-xs text-muted-foreground">
          Showing your most recently pushed repositories.
        </p>
      )}
    </div>
  );
}

/** Split `owner/name` into its parts. */
export function splitRepository(fullName: string): { owner: string; repo: string } {
  const [owner = "", repo = ""] = fullName.split("/", 2);
  return { owner, repo };
}
