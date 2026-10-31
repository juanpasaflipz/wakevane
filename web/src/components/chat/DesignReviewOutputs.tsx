import { useState } from "react";
import { Link } from "@/lib/routing";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CheckIcon,
  ChevronDownIcon,
  DownloadIcon,
  FileTextIcon,
  GitPullRequestIcon,
} from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import {
  type DesignApproval,
  approveDesignSpec,
  listDesignApprovals,
  openDesignPullRequest,
} from "@/lib/designReviewCodingApi";
import {
  type DesignReviewOutputFile,
  downloadDesignReviewOutput,
  getDesignReviewOutputs,
} from "@/lib/designReviewOutputsApi";

const REFRESH_MS = 30_000;

interface DesignReviewOutputsProps {
  sessionId: string;
  /** Approved coding is enabled on this server. */
  codingEnabled?: boolean;
  /** The viewer owns the review and may approve or open pull requests. */
  isOwner?: boolean;
}

/**
 * Saved results of a cloud Design review. They are copied out of the cloud
 * workspace while it runs, so they stay available after it shuts down. With
 * approved coding enabled, the owner can approve a saved specification.
 */
export function DesignReviewOutputs({
  sessionId,
  codingEnabled = false,
  isOwner = false,
}: DesignReviewOutputsProps) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const queryClient = useQueryClient();
  const outputs = useQuery({
    queryKey: ["design-review-outputs", sessionId],
    queryFn: () => getDesignReviewOutputs(sessionId),
    refetchInterval: REFRESH_MS,
  });
  const approvals = useQuery({
    queryKey: ["design-review-approvals", sessionId],
    queryFn: () => listDesignApprovals(sessionId),
    refetchInterval: REFRESH_MS,
    enabled: codingEnabled,
  });

  async function download(file: DesignReviewOutputFile) {
    try {
      const blob = await downloadDesignReviewOutput(sessionId, file);
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = file.path.split("/").pop() ?? "review.md";
      link.click();
      URL.revokeObjectURL(url);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Download failed");
    }
  }

  async function run(key: string, action: () => Promise<DesignApproval>, done: string) {
    setBusy(key);
    try {
      await action();
      toast.success(done);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Request failed");
    } finally {
      setBusy(null);
      await queryClient.invalidateQueries({ queryKey: ["design-review-approvals", sessionId] });
    }
  }

  const data = outputs.data;
  const summary = outputs.isError
    ? "Saved results are unavailable right now"
    : !data
      ? "Cloud review — results are saved as the review writes them"
      : `${data.files.length} saved ${data.files.length === 1 ? "file" : "files"} · ` +
        `updated ${new Date(data.publishedAt * 1000).toLocaleString()}`;
  const approved = new Map((approvals.data ?? []).map((item) => [item.specPath, item]));
  const canApprove = codingEnabled && isOwner;

  return (
    <Collapsible
      open={open}
      onOpenChange={setOpen}
      className="border-b border-border px-4 py-2 text-sm"
      data-testid="design-review-outputs"
    >
      <CollapsibleTrigger
        className="flex w-full items-center gap-2 text-left text-muted-foreground"
        disabled={!data}
      >
        <FileTextIcon className="size-4 shrink-0" aria-hidden />
        <span className="flex-1 truncate">{summary}</span>
        {data && (
          <ChevronDownIcon
            className={`size-4 shrink-0 transition-transform ${open ? "rotate-180" : ""}`}
            aria-hidden
          />
        )}
      </CollapsibleTrigger>
      {data && (
        <CollapsibleContent className="pt-2">
          {data.commit && (
            <p className="pb-2 text-xs text-muted-foreground">
              Reviewed commit <code>{data.commit.slice(0, 12)}</code>
              {data.repository ? ` of ${data.repository}` : ""}
            </p>
          )}
          <ul className="flex flex-col gap-1">
            {data.files.map((file) => (
              <li key={file.path} className="flex items-center gap-2">
                <span className="flex-1 truncate font-mono text-xs">{file.path}</span>
                {canApprove &&
                  file.path.endsWith(".md") &&
                  approved.get(file.path)?.specSha256 !== file.sha256 && (
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={busy !== null}
                      onClick={() =>
                        void run(
                          `approve:${file.path}`,
                          () =>
                            approveDesignSpec(sessionId, {
                              path: file.path,
                              sha256: file.sha256,
                              commit: data.commit,
                            }),
                          "Approved — a coding session is starting",
                        )
                      }
                    >
                      <CheckIcon className="size-4" aria-hidden />
                      Approve for coding
                    </Button>
                  )}
                <Button
                  variant="ghost"
                  size="sm"
                  aria-label={`Download ${file.path}`}
                  onClick={() => void download(file)}
                >
                  <DownloadIcon className="size-4" aria-hidden />
                </Button>
              </li>
            ))}
          </ul>
          {codingEnabled && (approvals.data?.length ?? 0) > 0 && (
            <ul className="mt-3 flex flex-col gap-2 border-t border-border pt-2">
              {approvals.data?.map((approval) => (
                <li key={approval.id} className="flex flex-col gap-1">
                  <div className="flex items-center gap-2">
                    <span className="flex-1 truncate text-xs">
                      Approved <code>{approval.specPath}</code> →{" "}
                      <code>{approval.codingBranch}</code>
                    </span>
                    {approval.codingSessionId && (
                      <Link className="text-xs underline" to={`/c/${approval.codingSessionId}`}>
                        Coding session
                      </Link>
                    )}
                    {approval.pullRequest ? (
                      <a
                        className="text-xs underline"
                        href={approval.pullRequest.url}
                        target="_blank"
                        rel="noreferrer"
                      >
                        Pull request #{approval.pullRequest.number}
                      </a>
                    ) : (
                      isOwner &&
                      approval.codingSessionId && (
                        <Button
                          variant="outline"
                          size="sm"
                          disabled={busy !== null || !approval.codingOutputs}
                          onClick={() =>
                            void run(
                              `pr:${approval.id}`,
                              () => openDesignPullRequest(sessionId, approval.id),
                              "Pull request opened",
                            )
                          }
                        >
                          <GitPullRequestIcon className="size-4" aria-hidden />
                          Open pull request
                        </Button>
                      )
                    )}
                  </div>
                  <p className="text-xs text-muted-foreground">
                    {approval.codingError
                      ? `Coding session did not start: ${approval.codingError}`
                      : approval.codingOutputs
                        ? `${approval.codingOutputs.changedFiles} changed, ` +
                          `${approval.codingOutputs.deletedFiles} deleted · ` +
                          (approval.codingOutputs.hasTestOutput
                            ? "test output saved"
                            : "no test output yet") +
                          ` · saved ${new Date(
                            approval.codingOutputs.publishedAt * 1000,
                          ).toLocaleString()}`
                        : "Waiting for the coding session to save changes"}
                  </p>
                </li>
              ))}
            </ul>
          )}
        </CollapsibleContent>
      )}
    </Collapsible>
  );
}
