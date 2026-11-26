/** Locating and opening a piece of evidence. Nothing is inferred. */
import type { CaptureDetail, Evidence } from "../contracts/metis";
import { desktopCommand } from "./desktop";

/** Plan/task refs carry only the source pointer; search and plan citations add the rest. */
export type EvidenceRef = { source_id: string; source_version: number; span_ref?: string | null } & Partial<
  Pick<Evidence, "origin" | "title" | "recorded_at" | "uri" | "excerpt" | "start_char" | "end_char" | "matched" | "chunk_id">
>;

export type Matcher = "semantic" | "keyword";

const ORIGIN_LABEL = { memory: "Memory", file: "File", email: "Email", drive: "Drive" } as const;

export function originLabel(ref: EvidenceRef): string {
  return ORIGIN_LABEL[ref.origin ?? "memory"];
}

/** Corpus citations (files, mail, Drive) open their original rather than a capture transcript. */
export function isCorpus(ref: EvidenceRef): boolean {
  return !!ref.origin && ref.origin !== "memory";
}

/**
 * Which matchers hit. Tolerant of shape: the generated contract type for
 * `matched` is malformed (`"semantic" | "keyword"[]`), the wire value is an array.
 */
export function matchedOf(ref: EvidenceRef): Matcher[] {
  const raw: unknown = ref.matched;
  const list = Array.isArray(raw) ? raw : typeof raw === "string" ? [raw] : [];
  return (["semantic", "keyword"] as const).filter((m) => list.includes(m));
}

/** A local filesystem path for a file citation, from a `file://` URI or a bare path; else null. */
export function pathFromUri(uri: string | null | undefined): string | null {
  if (!uri) return null;
  if (uri.startsWith("file://")) {
    try {
      const path = decodeURIComponent(new URL(uri).pathname);
      return /^\/[A-Za-z]:\//.test(path) ? path.slice(1) : path;
    } catch {
      return null;
    }
  }
  return uri.startsWith("/") || /^[A-Za-z]:[\\/]/.test(uri) || uri.startsWith("\\\\") ? uri : null;
}

/** An http(s) deep link (Gmail / Drive), else null. */
export function webLink(uri: string | null | undefined): string | null {
  return uri && /^https?:\/\//i.test(uri) ? uri : null;
}

/** Reveal a cited file in the OS file manager, or copy its path when that is not available. */
export async function revealOrCopy(path: string): Promise<"revealed" | "copied" | "failed"> {
  if ((await desktopCommand<void>("reveal_path", { path })).ok) return "revealed";
  try {
    await navigator.clipboard.writeText(path);
    return "copied";
  } catch {
    return "failed";
  }
}

export type Located = { before: string; match: string; after: string } | { text: string; span: null } | null;

/** The transcript revision the evidence cites, with the cited span marked when the server gave one. */
export function locate(detail: CaptureDetail, ref: EvidenceRef): Located {
  const revision = (detail.source.transcripts ?? []).find((t) => t.source_version === ref.source_version);
  const text = revision?.text ?? (ref.source_version === detail.source.current_version ? detail.transcript_text : null);
  if (!text) return null;
  const span = ref.span_ref ? (detail.source.spans ?? []).find((s) => s.span_ref === ref.span_ref && s.source_version === ref.source_version) : null;
  if (!span || span.end_char > text.length || span.start_char >= span.end_char) return { text, span: null };
  return { before: text.slice(0, span.start_char), match: text.slice(span.start_char, span.end_char), after: text.slice(span.end_char) };
}
