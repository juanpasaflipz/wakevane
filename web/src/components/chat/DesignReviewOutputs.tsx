import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronDownIcon, DownloadIcon, FileTextIcon } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import {
  type DesignReviewOutputFile,
  downloadDesignReviewOutput,
  getDesignReviewOutputs,
} from "@/lib/designReviewOutputsApi";

const REFRESH_MS = 30_000;

/**
 * Saved results of a cloud Design review. They are copied out of the cloud
 * workspace while it runs, so they stay available after it shuts down.
 */
export function DesignReviewOutputs({ sessionId }: { sessionId: string }) {
  const [open, setOpen] = useState(false);
  const outputs = useQuery({
    queryKey: ["design-review-outputs", sessionId],
    queryFn: () => getDesignReviewOutputs(sessionId),
    refetchInterval: REFRESH_MS,
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

  const data = outputs.data;
  const summary = outputs.isError
    ? "Saved results are unavailable right now"
    : !data
      ? "Cloud review — results are saved as the review writes them"
      : `${data.files.length} saved ${data.files.length === 1 ? "file" : "files"} · ` +
        `updated ${new Date(data.publishedAt * 1000).toLocaleString()}`;

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
        </CollapsibleContent>
      )}
    </Collapsible>
  );
}
