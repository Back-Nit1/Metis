import { describe, expect, it } from "vitest";
import { isCorpus, matchedOf, originLabel, pathFromUri, webLink, type EvidenceRef } from "./evidence";

const base: EvidenceRef = { source_id: "s1", source_version: 1 };

describe("citation helpers", () => {
  it("labels origins and defaults to Memory", () => {
    expect(originLabel(base)).toBe("Memory");
    expect(originLabel({ ...base, origin: "drive" })).toBe("Drive");
    expect(isCorpus(base)).toBe(false);
    expect(isCorpus({ ...base, origin: "file" })).toBe(true);
  });

  it("reads matched as an array whatever the generated type says", () => {
    expect(matchedOf({ ...base, matched: ["keyword", "semantic"] as never })).toEqual(["semantic", "keyword"]);
    expect(matchedOf({ ...base, matched: "keyword" as never })).toEqual(["keyword"]);
    expect(matchedOf(base)).toEqual([]);
  });

  it("turns file URIs and bare paths into paths, and nothing else", () => {
    expect(pathFromUri("file:///Users/me/My%20Notes/a.md")).toBe("/Users/me/My Notes/a.md");
    expect(pathFromUri("file:///C:/docs/a.txt")).toBe("C:/docs/a.txt");
    expect(pathFromUri("/tmp/x.pdf")).toBe("/tmp/x.pdf");
    expect(pathFromUri("C:\\docs\\a.txt")).toBe("C:\\docs\\a.txt");
    expect(pathFromUri("https://drive.google.com/x")).toBeNull();
    expect(pathFromUri(null)).toBeNull();
    expect(webLink("https://mail.google.com/x")).toBe("https://mail.google.com/x");
    expect(webLink("javascript:alert(1)")).toBeNull();
  });
});
