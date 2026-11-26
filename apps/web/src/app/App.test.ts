import { describe, expect, it } from "vitest";
import { appRouteFromHash } from "./App";

describe("appRouteFromHash", () => {
  it("keeps the legacy notifications link working", () => {
    expect(appRouteFromHash("#/notifications")).toBe("alerts");
  });

  it("does not silently turn unknown routes into Today", () => {
    expect(appRouteFromHash("#/not-a-screen")).toBe("not-found");
  });

  it("resolves every current app screen", () => {
    expect(appRouteFromHash("#/today")).toBe("today");
    expect(appRouteFromHash("#/inbox")).toBe("inbox");
    expect(appRouteFromHash("#/settings")).toBe("settings");
  });
});
