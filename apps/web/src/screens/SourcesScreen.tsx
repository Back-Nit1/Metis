/**
 * Sources: the folders (and later mail / Drive) Metis indexes, the ingest
 * queue, and anything that failed for good.
 *
 * Every state is the server's: a sync request is "queued", not "synced"; a
 * removed folder is gone now but its index is purged in the background; queue
 * counts come from the sync worker, not from guessing.
 */
import { useCallback, useEffect, useState } from "react";
import { ConnectorStatus, type Connector, type DeadLetter } from "../contracts/metis";
import { Button } from "../components/Button";
import { Card, Chip, Empty, Loading, Notice, PageHeader, SectionHeading } from "../components/Card";
import { Field, inputClass } from "../components/Field";
import { useSession } from "../app/session";
import { api } from "../lib/api";
import { isHostedDesktop, desktopCommand, isDesktop } from "../lib/desktop";
import { timeLabel } from "../lib/format";
import { mutationError } from "../lib/mutationError";
import {
  buildFolderRequest,
  connectorState,
  ingestBacklog,
  keywordOnly,
  lane,
  totalDeadLetters,
  type FolderDraft,
  type OllamaStatus,
} from "../lib/sources";
import { useChanges, useSync } from "../lib/useSync";

const hostedSources = () => isHostedDesktop() || !!import.meta.env.VITE_METIS_FIREBASE_PROJECT_ID;

const EMPTY_DRAFT: FolderDraft = { title: "", paths: "", include: "", exclude: "" };
const KIND_LABEL: Record<string, string> = { local_folder: "Folder", gmail: "Gmail", gdrive: "Drive" };
const TONE_CLASS = { good: "text-good", warn: "text-warn", bad: "text-bad", info: "text-muted" } as const;

type Say = { tone: "good" | "warn" | "bad" | "info"; text: string } | null;
type SyncReport = { listed: number; uploaded: number; failed: number; tombstoned: number; rejected: number };

/**
 * The shared server never reads this Mac's disk: the desktop app lists the
 * folder and uploads what changed. Sent is not indexed, so it says "sent".
 */
async function syncFromDesktop(c: Connector): Promise<Say> {
  const r = await desktopCommand<SyncReport>("sync_folder", { connectorId: c.id, paths: c.config?.paths ?? [] });
  if (!r.ok) return { tone: "bad", text: `“${c.title}” did not sync: ${r.error ?? "the desktop app could not read the folder"}.` };
  const { listed, uploaded, failed, tombstoned } = r.value;
  const parts = [`Sent ${uploaded} changed file${uploaded === 1 ? "" : "s"} of ${listed} from “${c.title}”`];
  if (tombstoned) parts.push(`${tombstoned} removed`);
  if (failed) parts.push(`${failed} could not be sent`);
  return { tone: failed ? "warn" : "info", text: `${parts.join("; ")}. Sent files become searchable as they are indexed.` };
}

function AddFolder({ onAdded }: { onAdded: (c: Connector) => void }) {
  const [draft, setDraft] = useState<FolderDraft>(EMPTY_DRAFT);
  const [key, setKey] = useState(() => crypto.randomUUID());
  const [busy, setBusy] = useState(false);
  const [say, setSay] = useState<Say>(null);
  const [picker, setPicker] = useState(isDesktop());
  const set = (patch: Partial<FolderDraft>) => setDraft((d) => ({ ...d, ...patch }));

  const pick = async () => {
    const result = await desktopCommand<string | null>("pick_folder");
    if (!result.ok) {
      setPicker(false);
      return setSay({ tone: "info", text: "The folder picker is not available here; type the full path instead." });
    }
    const path = result.value;
    if (!path) return;
    setDraft((d) => ({
      ...d,
      paths: d.paths.trim() ? `${d.paths.trim()}\n${path}` : path,
      title: d.title || path.split(/[\\/]/).filter(Boolean).pop() || "",
    }));
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const built = buildFolderRequest(draft);
    if ("problem" in built) return setSay({ tone: "warn", text: built.problem });
    setBusy(true);
    setSay(null);
    try {
      const created = await api.createConnector(built.request, key);
      setDraft(EMPTY_DRAFT);
      setKey(crypto.randomUUID());
      setSay(
        isHostedDesktop()
          ? await syncFromDesktop(created)
          : { tone: "good", text: `Added “${created.title}”. The first sync is queued; files become searchable as they are indexed.` },
      );
      onAdded(created);
    } catch (cause) {
      setSay({ tone: "bad", text: mutationError("The folder", cause).text });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <SectionHeading hint="read-only; nothing in the folder is changed">Add a folder</SectionHeading>
      <form onSubmit={(e) => void submit(e)} className="flex flex-col gap-3">
        <Field label="Name">
          <input className={inputClass} value={draft.title} onChange={(e) => set({ title: e.target.value })} placeholder="Work notes" />
        </Field>
        <Field label="Folder paths" hint="Absolute paths, one per line.">
          <textarea
            className={`${inputClass} min-h-20 font-mono text-xs`}
            value={draft.paths}
            onChange={(e) => set({ paths: e.target.value })}
            placeholder="/Users/you/Documents/notes"
            spellCheck={false}
          />
        </Field>
        {picker && (
          <div>
            <Button onClick={() => void pick()}>Choose folder…</Button>
          </div>
        )}
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Only include (optional)" hint="Globs, comma or line separated, e.g. *.md, *.pdf">
            <input className={`${inputClass} font-mono text-xs`} value={draft.include} onChange={(e) => set({ include: e.target.value })} spellCheck={false} />
          </Field>
          <Field label="Exclude (optional)" hint="Added to the server's default excludes.">
            <input className={`${inputClass} font-mono text-xs`} value={draft.exclude} onChange={(e) => set({ exclude: e.target.value })} spellCheck={false} />
          </Field>
        </div>
        {say && <Notice tone={say.tone} live>{say.text}</Notice>}
        <div>
          <Button variant="primary" type="submit" disabled={busy}>
            {busy ? "Adding…" : "Add folder"}
          </Button>
        </div>
      </form>
    </Card>
  );
}

function ConnectorRow({
  c,
  backlog,
  timezone,
  onChanged,
  onConflict,
}: {
  c: Connector;
  backlog: number | null;
  timezone: string;
  onChanged: (say: Say) => void;
  onConflict: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const state = connectorState(c, backlog);
  const paused = c.status === ConnectorStatus.PAUSED;

  const run = async (what: string, action: () => Promise<Say>) => {
    setBusy(true);
    try {
      onChanged(await action());
    } catch (cause) {
      const err = mutationError(what, cause);
      if (err.conflict) onConflict();
      onChanged({ tone: err.conflict ? "warn" : "bad", text: err.text });
    } finally {
      setBusy(false);
      setConfirming(false);
    }
  };

  const toggle = () =>
    run("This source", async () => {
      await api.patchConnector(c.id, { expected_revision: c.revision ?? 1, status: paused ? "active" : "paused" });
      return { tone: "good", text: paused ? `Resumed “${c.title}”.` : `Paused “${c.title}”. Already indexed files stay searchable.` };
    });
  const syncNow = () =>
    run("This source", async () => {
      if (isHostedDesktop()) return syncFromDesktop(c);
      await api.syncConnector(c.id, crypto.randomUUID());
      return { tone: "info", text: `Sync of “${c.title}” is queued. It has not run yet.` };
    });
  // A hosted folder lives on someone's Mac; only the desktop app can sync it.
  const canSync = !hostedSources() || isHostedDesktop();
  const remove = () =>
    run("This source", async () => {
      await api.deleteConnector(c.id);
      return { tone: "info", text: `Removed “${c.title}”. Its files are no longer searchable; the index copies are purged in the background.` };
    });

  return (
    <li className="rounded-lg border border-line bg-surface p-4">
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
        <div className="min-w-0 flex-1 basis-60">
          <p className="flex flex-wrap items-center gap-2">
            <span className="font-serif text-base font-semibold text-ink">{c.title}</span>
            <Chip>{KIND_LABEL[c.kind] ?? c.kind}</Chip>
            <span className={`text-xs font-medium ${TONE_CLASS[state.tone]}`}>{state.word}</span>
          </p>
          {(c.config?.paths ?? []).map((p) => (
            <p key={p} className="break-all font-mono text-2xs text-muted">{p}</p>
          ))}
          <p className="mt-1 text-xs text-muted">
            {c.item_count ?? 0} item{(c.item_count ?? 0) === 1 ? "" : "s"} indexed ·{" "}
            {c.last_sync_at ? `last sync ${timeLabel(c.last_sync_at, timezone)}` : "never synced"}
          </p>
          {c.last_error && <p className="mt-1 text-xs text-bad">Last error: {c.last_error}</p>}
          {c.status === ConnectorStatus.NEEDS_AUTH && (
            <p className="mt-1 text-xs text-warn">Access was revoked or expired. Reconnect this source to resume syncing.</p>
          )}
        </div>
        <div className="flex flex-wrap gap-2">
          {canSync ? (
            <Button disabled={busy || paused} onClick={() => void syncNow()}>Sync now</Button>
          ) : (
            <span className="self-center text-xs text-muted">Syncs from the Metis desktop app</span>
          )}
          <Button disabled={busy} onClick={() => void toggle()}>{paused ? "Resume" : "Pause"}</Button>
          {!confirming && <Button variant="danger" disabled={busy} onClick={() => setConfirming(true)}>Remove…</Button>}
        </div>
      </div>
      {confirming && (
        <Notice
          tone="warn"
          className="mt-3"
          action={
            <div className="flex gap-2">
              <Button variant="danger" disabled={busy} onClick={() => void remove()}>{busy ? "Removing…" : "Remove and purge"}</Button>
              <Button variant="quiet" onClick={() => setConfirming(false)}>Keep it</Button>
            </div>
          }
        >
          Metis stops syncing this source and deletes what it indexed from it. Your original files are not touched.
        </Notice>
      )}
    </li>
  );
}

function DeadLetterRow({ d, timezone, onRetried }: { d: DeadLetter; timezone: string; onRetried: (say: Say) => void }) {
  const [busy, setBusy] = useState(false);
  const retry = async () => {
    setBusy(true);
    try {
      // updated_at changes when a retry fails again, so the next attempt gets a new key.
      await api.retryDeadLetter(d.event_id, `dlq:${d.event_id}:${d.updated_at}`.replace(/[^A-Za-z0-9_.:=-]/g, "-").slice(0, 128));
      onRetried({ tone: "info", text: `Queued “${d.title ?? d.event_type}” again. It has not run yet.` });
    } catch (cause) {
      onRetried({ tone: "bad", text: mutationError("This item", cause).text });
    } finally {
      setBusy(false);
    }
  };
  return (
    <li className="flex flex-wrap items-start justify-between gap-3 rounded-lg border border-line bg-surface p-3">
      <div className="min-w-0 flex-1 basis-60 text-xs text-muted">
        <p className="text-sm font-medium text-ink">{d.title ?? d.target_id ?? d.event_type}</p>
        <p className="font-mono text-2xs">
          {d.event_type} · {d.lane} lane · {d.attempts} attempt{d.attempts === 1 ? "" : "s"} · {timeLabel(d.updated_at, timezone)}
        </p>
        {d.failure && <p className="mt-1">Failure: {d.failure}</p>}
        {d.error_message && <p className="mt-0.5 break-words text-bad">{d.error_message}</p>}
      </div>
      <Button disabled={busy} onClick={() => void retry()}>{busy ? "Retrying…" : "Retry"}</Button>
    </li>
  );
}

export function SourcesScreen() {
  const { timezone } = useSession();
  const sync = useSync();
  const [connectors, setConnectors] = useState<Connector[] | null>(null);
  const [dead, setDead] = useState<DeadLetter[]>([]);
  const [loadError, setLoadError] = useState(false);
  const [say, setSay] = useState<Say>(null);
  const [ollama, setOllama] = useState<OllamaStatus | null>(null);
  const [fetchedQueue, setFetchedQueue] = useState<Awaited<ReturnType<typeof api.queue>> | null>(null);
  const queue = sync.queue ?? fetchedQueue;

  const loadConnectors = useCallback(async () => {
    try {
      setConnectors((await api.listConnectors()).items);
      setLoadError(false);
    } catch {
      setLoadError(true);
    }
  }, []);
  const loadDead = useCallback(async () => {
    try {
      setDead((await api.deadLetters()).items ?? []);
    } catch {
      /* the list keeps its last value; the queue panel still shows the count */
    }
  }, []);

  useEffect(() => {
    void loadConnectors();
    api.queue().then(setFetchedQueue, () => undefined);
    if (!hostedSources()) void desktopCommand<OllamaStatus>("ollama_status").then((r) => r.ok && setOllama(r.value));
  }, [loadConnectors]);
  useChanges(["connector", "source"], () => void loadConnectors());
  // Dead letters are not entities in the change feed; their count in the queue stats is.
  const deadCount = totalDeadLetters(queue);
  useEffect(() => void loadDead(), [loadDead, deadCount]);

  const backlog = ingestBacklog(queue);
  const afterAction = (next: Say) => (setSay(next), void loadConnectors(), void loadDead());

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Sources"
        sub="Folders Metis reads and indexes so search and plans can cite them. Originals are never changed."
      />
      {say && <Notice tone={say.tone} live>{say.text}</Notice>}
      {!hostedSources() && keywordOnly(ollama) && (
        <Notice tone="warn">
          Keyword search only: {ollama?.available ? "the embedding model is not installed in Ollama" : "Ollama is not running"}, so files are matched by words and names, not meaning.
        </Notice>
      )}
      {(sync.status === "offline" || sync.status === "unauthorized" || sync.status === "unsupported") && (
        <Notice tone="info">
          {sync.status === "unsupported"
            ? "Live updates are not supported in this browser; reopen this screen to see new progress."
            : sync.status === "unauthorized"
              ? "Live updates stopped: your session was refused. Sign in again to resume them."
              : `Live updates are paused; the server is unreachable${sync.retryInMs ? `, retrying in ${Math.round(sync.retryInMs / 1000)}s` : ""}. What is shown may be out of date.`}
        </Notice>
      )}

      <div className="lg:grid lg:grid-cols-[minmax(0,1fr)_20rem] lg:items-start lg:gap-8">
        <div className="flex flex-col gap-6">
          <section aria-labelledby="h-sources" className="flex flex-col gap-3">
            <SectionHeading id="h-sources" hint={connectors ? `${connectors.length} connected` : undefined}>
              Connected sources
            </SectionHeading>
            {loadError && (
              <Notice tone="bad" live action={<Button onClick={() => void loadConnectors()}>Try again</Button>}>
                Sources could not be loaded. Nothing was changed.
              </Notice>
            )}
            {!connectors && !loadError && <Loading label="Loading sources" rows={2} />}
            {connectors?.length === 0 && (
              <Empty icon="search" title="No sources yet">
                Add a folder below and its notes, documents and PDFs become searchable alongside what you capture.
              </Empty>
            )}
            {connectors && connectors.length > 0 && (
              <ul className="flex flex-col gap-2">
                {connectors.map((c) => (
                  <ConnectorRow
                    key={c.id}
                    c={c}
                    backlog={backlog}
                    timezone={timezone}
                    onChanged={afterAction}
                    onConflict={() => void loadConnectors()}
                  />
                ))}
              </ul>
            )}
          </section>
          {(!hostedSources() || isHostedDesktop()) && <AddFolder onAdded={() => void loadConnectors()} />}
        </div>

        <aside className="mt-6 flex flex-col gap-6 lg:mt-0">
          <Card>
            <SectionHeading hint={queue ? (backlog ? "Indexing…" : "idle") : "unknown"}>Queue</SectionHeading>
            {queue ? (
              <table className="w-full text-xs">
                <thead className="text-left text-muted">
                  <tr>
                    <th className="py-1 font-medium">Lane</th>
                    <th className="py-1 text-right font-medium">Waiting</th>
                    <th className="py-1 text-right font-medium">Running</th>
                    <th className="py-1 text-right font-medium">Retrying</th>
                    <th className="py-1 text-right font-medium">Failed</th>
                  </tr>
                </thead>
                <tbody className="font-mono">
                  {(["interactive", "ingest"] as const).map((name) => {
                    const l = lane(queue, name);
                    return (
                      <tr key={name} className="border-t border-line">
                        <td className="py-1.5 font-sans">{name === "ingest" ? "Indexing" : "Captures & reminders"}</td>
                        <td className="py-1.5 text-right">{l.pending ?? 0}</td>
                        <td className="py-1.5 text-right">{l.leased ?? 0}</td>
                        <td className="py-1.5 text-right">{l.failed ?? 0}</td>
                        <td className="py-1.5 text-right">{l.dead_letter ?? 0}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            ) : (
              <p className="text-xs text-muted">Queue counts are not available yet.</p>
            )}
          </Card>

          <section aria-labelledby="h-dead" className="flex flex-col gap-2">
            <SectionHeading id="h-dead" hint={`${dead.length} shown`}>
              Failed for good
            </SectionHeading>
            {dead.length === 0 ? (
              <p className="text-xs text-muted">Nothing has failed permanently.</p>
            ) : (
              <ul className="flex flex-col gap-2">
                {dead.map((d) => (
                  <DeadLetterRow key={d.event_id} d={d} timezone={timezone} onRetried={afterAction} />
                ))}
              </ul>
            )}
          </section>
        </aside>
      </div>
    </div>
  );
}
