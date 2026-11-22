import { describe, expect, it } from "vitest";
import { ConnectorKind, ConnectorStatus, type Connector } from "../contracts/metis";
import { buildFolderRequest, connectorState, ingestBacklog, keywordOnly, parseList, totalDeadLetters } from "./sources";

const connector = (over: Partial<Connector> = {}): Connector => ({
  id: "c1",
  user_id: "u",
  created_at: "2026-11-21T00:00:00Z",
  updated_at: "2026-11-21T00:00:00Z",
  kind: ConnectorKind.LOCAL_FOLDER,
  title: "Notes",
  ...over,
});

describe("folder form", () => {
  it("parses globs by line or comma", () => {
    expect(parseList("*.md, *.txt\n\n*.md")).toEqual(["*.md", "*.txt"]);
  });

  it("builds a local_folder request with only the globs given", () => {
    expect(buildFolderRequest({ title: " Notes ", paths: "/Users/me/a, b\n/Users/me/c\n", include: "", exclude: "node_modules/**" })).toEqual({
      request: {
        kind: "local_folder",
        title: "Notes",
        config: { paths: ["/Users/me/a, b", "/Users/me/c"], exclude_globs: ["node_modules/**"] },
      },
    });
  });

  it("refuses relative paths and empty fields without guessing", () => {
    expect(buildFolderRequest({ title: "", paths: "/a", include: "", exclude: "" })).toEqual({ problem: "Give the folder a name." });
    expect(buildFolderRequest({ title: "x", paths: "  ", include: "", exclude: "" })).toEqual({ problem: "Add at least one folder path." });
    expect(buildFolderRequest({ title: "x", paths: "notes", include: "", exclude: "" })).toEqual({ problem: '"notes" is not an absolute path.' });
    expect("request" in buildFolderRequest({ title: "x", paths: "C:\\Docs", include: "", exclude: "" })).toBe(true);
  });
});

describe("honest states", () => {
  const queue = (pending: number) => ({ lanes: [{ lane: "ingest" as const, pending, leased: 1, dead_letter: 2 }, { lane: "interactive" as const, dead_letter: 1 }] });

  it("counts backlog and dead letters, and knows when it does not know", () => {
    expect(ingestBacklog(null)).toBeNull();
    expect(ingestBacklog(queue(3))).toBe(4);
    expect(totalDeadLetters(queue(0))).toBe(3);
  });

  it("never says up to date before a sync finished or while ingest is busy", () => {
    expect(connectorState(connector(), 0).word).toBe("Indexing…");
    expect(connectorState(connector({ last_sync_at: "2026-11-21T01:00:00Z" }), 5).word).toBe("Indexing…");
    expect(connectorState(connector({ last_sync_at: "2026-11-21T01:00:00Z" }), 0).word).toBe("Up to date");
    expect(connectorState(connector({ last_sync_at: "2026-11-21T01:00:00Z" }), null).word).toBe("Synced");
    expect(connectorState(connector({ status: ConnectorStatus.NEEDS_AUTH }), 0).word).toBe("Needs re-auth");
    expect(connectorState(connector({ status: ConnectorStatus.PAUSED }), 0).word).toBe("Paused");
    expect(connectorState(connector({ status: ConnectorStatus.ERROR }), 0).tone).toBe("bad");
  });

  it("says keyword only only when the desktop reports no embedding model", () => {
    expect(keywordOnly(null)).toBe(false);
    expect(keywordOnly({ available: true, models: [], embed_model_present: true })).toBe(false);
    expect(keywordOnly({ available: false, models: [], embed_model_present: false })).toBe(true);
    expect(keywordOnly({ available: true, models: ["llama"], embed_model_present: false })).toBe(true);
  });
});
