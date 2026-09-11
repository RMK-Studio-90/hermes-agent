// @vitest-environment jsdom
//
// Phase 2 — profile-scoped model picker.
//
// The default Models page and the profile editors now share ONE inventory
// (`api.getModelOptions`) and ONE assignment path (`api.setModelAssignment`).
// These assert that an explicitly-supplied profile always wins over the
// globally-active management profile, for every operation the picker performs.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api, setManagementProfile } from "./api";

function jsonFetchMock(body: unknown = { ok: true }) {
  return vi.fn<typeof fetch>(
    async () =>
      new Response(JSON.stringify(body), {
        headers: { "Content-Type": "application/json" },
        status: 200,
      }),
  );
}

beforeEach(() => {
  Object.defineProperty(window, "__HERMES_SESSION_TOKEN__", {
    configurable: true,
    value: "t",
    writable: true,
  });
});

afterEach(() => {
  setManagementProfile("");
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("getModelOptions — explicit profile scope", () => {
  it("targets ?profile=<name> for the edited profile", async () => {
    const fetchMock = jsonFetchMock({ providers: [] });
    vi.stubGlobal("fetch", fetchMock);

    await api.getModelOptions({ profile: "rmk-intel" });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/model/options?profile=rmk-intel&include_unconfigured=1",
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("keeps the edited profile when refreshing (model discovery)", async () => {
    const fetchMock = jsonFetchMock({ providers: [] });
    vi.stubGlobal("fetch", fetchMock);

    await api.getModelOptions({ profile: "rmk-review", refresh: true });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/model/options?profile=rmk-review&refresh=1&include_unconfigured=1",
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("does NOT let the globally-active sidebar profile override an explicit one", async () => {
    setManagementProfile("rmk-growth"); // sidebar selection
    const fetchMock = jsonFetchMock({ providers: [] });
    vi.stubGlobal("fetch", fetchMock);

    await api.getModelOptions({ profile: "rmk-intel" });

    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain("profile=rmk-intel");
    expect(url).not.toContain("profile=rmk-growth");
  });

  it("distinct profiles produce distinct requests", async () => {
    const fetchMock = jsonFetchMock({ providers: [] });
    vi.stubGlobal("fetch", fetchMock);

    await api.getModelOptions({ profile: "rmk-intel" });
    await api.getModelOptions({ profile: "rmk-review" });

    expect(fetchMock.mock.calls.map(([u]) => u)).toEqual([
      "/api/model/options?profile=rmk-intel&include_unconfigured=1",
      "/api/model/options?profile=rmk-review&include_unconfigured=1",
    ]);
  });
});

describe("setModelAssignment — profile-scoped rich assignment path", () => {
  it("POSTs to /api/model/set?profile=<name> with provider + model", async () => {
    const fetchMock = jsonFetchMock({ ok: true });
    vi.stubGlobal("fetch", fetchMock);

    await api.setModelAssignment(
      { scope: "main", provider: "moa", model: "cx/gpt-5.6-terra-high" },
      "rmk-intel",
    );

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/model/set?profile=rmk-intel");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toMatchObject({
      scope: "main",
      provider: "moa",
      model: "cx/gpt-5.6-terra-high",
    });
  });

  it("ignores the sidebar profile when an explicit target is given", async () => {
    setManagementProfile("rmk-growth");
    const fetchMock = jsonFetchMock({ ok: true });
    vi.stubGlobal("fetch", fetchMock);

    await api.setModelAssignment(
      { scope: "main", provider: "openrouter", model: "x/y" },
      "rmk-review",
    );

    expect(fetchMock.mock.calls[0][0]).toBe("/api/model/set?profile=rmk-review");
  });
});
