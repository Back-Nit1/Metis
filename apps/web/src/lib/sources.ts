/** Pure helpers for the Sources screen: form parsing and honest status words. */
import { ConnectorStatus, type Connector, type CreateConnectorRequest, type LaneStats, type QueueStats } from "../contracts/metis";

/** One entry per line or comma; blanks dropped, duplicates collapsed. */
export function parseList(text: string): string[] {
  return [...new Set(text.split(/[\n,]/).map((s) => s.trim()).filter(Boolean))];
}

export function isAbsolutePath(path: string): boolean {
  return path.startsWith("/") || /^[A-Za-z]:[\\/]/.test(path) || path.startsWith("\\\\");
}

export type FolderDraft = { title: string; paths: string; include: string; exclude: string };

/** The create request, or the one reason it cannot be sent. */
export function buildFolderRequest(draft: FolderDraft): { request: CreateConnectorRequest } | { problem: string } {
  const title = draft.title.trim();
  // Paths are one per line only: a folder name may contain a comma.
  const paths = [...new Set(draft.paths.split("\n").map((p) => p.trim()).filter(Boolean))];
  if (!title) return { problem: "Give the folder a name." };
  if (paths.length === 0) return { problem: "Add at least one folder path." };
  const relative = paths.find((p) => !isAbsolutePath(p));
  if (relative) return { problem: `"${relative}" is not an absolute path.` };
  const include = parseList(draft.include);
  const exclude = parseList(draft.exclude);
  return {
    request: {
      kind: "local_folder",
      title,
      config: {
        paths,
        ...(include.length ? { include_globs: include } : {}),
        ...(exclude.length ? { exclude_globs: exclude } : {}),
      },
    },
  };
}

export function lane(queue: QueueStats | null, name: LaneStats["lane"]): LaneStats {
  return queue?.lanes?.find((l) => l.lane === name) ?? { lane: name };
}

/** Ingest work waiting or running right now; null when the queue counts are unknown. */
export function ingestBacklog(queue: QueueStats | null): number | null {
  if (!queue) return null;
  const l = lane(queue, "ingest");
  return (l.pending ?? 0) + (l.leased ?? 0);
}

export function totalDeadLetters(queue: QueueStats | null): number {
  return (queue?.lanes ?? []).reduce((n, l) => n + (l.dead_letter ?? 0), 0);
}

export type ConnectorState = { word: string; tone: "good" | "warn" | "bad" | "info" };

/** What a connector's state honestly is. "Indexed" needs a completed sync, not just an accepted request. */
export function connectorState(c: Connector, backlog: number | null): ConnectorState {
  switch (c.status ?? ConnectorStatus.ACTIVE) {
    case ConnectorStatus.NEEDS_AUTH:
      return { word: "Needs re-auth", tone: "warn" };
    case ConnectorStatus.ERROR:
      return { word: "Sync failed", tone: "bad" };
    case ConnectorStatus.PAUSED:
      return { word: "Paused", tone: "info" };
    default:
      if (!c.last_sync_at || backlog) return { word: "Indexing…", tone: "info" };
      // The ingest backlog is shared by every connector, so "up to date" needs it empty.
      return backlog === 0 ? { word: "Up to date", tone: "good" } : { word: "Synced", tone: "good" };
  }
}

/** The desktop `ollama_status` answer, when the shell has that command. */
export type OllamaStatus = { available: boolean; models: string[]; embed_model_present: boolean };

export function keywordOnly(status: OllamaStatus | null): boolean {
  return !!status && (!status.available || !status.embed_model_present);
}
