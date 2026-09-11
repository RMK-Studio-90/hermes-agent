// @vitest-environment jsdom
//
// Phase 2 — the Profiles page edits a profile's main model through the SAME
// shared ModelPickerDialog the default Models page uses, and every call it
// makes is scoped to the profile whose card was clicked — never the
// globally-active management profile.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const apiMocks = vi.hoisted(() => ({
  getProfiles: vi.fn(),
  getActiveProfile: vi.fn(),
  getModelOptions: vi.fn(),
  setModelAssignment: vi.fn(),
  createProfile: vi.fn(),
  setProfileModel: vi.fn(),
}));

// The picker itself is exercised in ModelPickerDialog.test.tsx. Here we only
// care which props the page hands it — capture them.
const pickerProps = vi.hoisted(() => ({
  current: null as Record<string, unknown> | null,
}));
vi.mock("@/components/ModelPickerDialog", () => ({
  ModelPickerDialog: (props: Record<string, unknown>) => {
    pickerProps.current = props;
    return null;
  },
}));

vi.mock("@/lib/api", () => ({
  api: apiMocks,
  setManagementProfile: vi.fn(),
  getManagementProfile: vi.fn(() => "rmk-growth"), // sidebar selection ≠ edited profile
}));

let container: HTMLDivElement;
let root: Root;
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

async function waitFor(cond: () => boolean, timeoutMs = 4000) {
  const start = Date.now();
  while (!cond()) {
    if (Date.now() - start > timeoutMs) throw new Error("waitFor timed out");
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20));
    });
  }
}

function click(el: Element | null) {
  if (!el) throw new Error("element not rendered");
  el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
}

function findButton(text: string): HTMLButtonElement | null {
  return (
    Array.from(document.querySelectorAll("button")).find(
      (b) => b.textContent?.trim().toLowerCase() === text.toLowerCase(),
    ) ?? null
  );
}

async function openModelPickerFor(profileName: string) {
  const actionButtons = Array.from(
    document.querySelectorAll('button[aria-label="Actions"]'),
  );
  const idx = ["rmk-intel", "rmk-review"].indexOf(profileName);
  click(actionButtons[idx] ?? actionButtons[0]);
  await act(async () => {});
  click(findButton("Change model"));
  await act(async () => {});
}

beforeEach(() => {
  for (const fn of Object.values(apiMocks)) fn.mockReset();
  pickerProps.current = null;
  apiMocks.getProfiles.mockResolvedValue({
    profiles: [
      {
        name: "rmk-intel",
        path: "/p/rmk-intel",
        is_default: false,
        provider: "moa",
        model: "cx/gpt-5.6-terra-high",
        has_env: false,
        skill_count: 0,
        gateway_running: false,
        description: "",
        description_auto: false,
        display_name: "",
      },
      {
        name: "rmk-review",
        path: "/p/rmk-review",
        is_default: false,
        provider: "moa",
        model: "cc/claude-sonnet-5",
        has_env: false,
        skill_count: 0,
        gateway_running: false,
        description: "",
        description_auto: false,
        display_name: "",
      },
    ],
  });
  apiMocks.getActiveProfile.mockResolvedValue({ active: "default" });
  apiMocks.getModelOptions.mockResolvedValue({
    providers: [],
    model: "",
    provider: "",
  });
  apiMocks.setModelAssignment.mockResolvedValue({ ok: true });
  vi.stubGlobal("matchMedia", () => ({
    addEventListener() {},
    matches: false,
    media: "",
    removeEventListener() {},
  }));
});

afterEach(async () => {
  await act(async () => root?.unmount());
  container?.remove();
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
});

async function renderPage() {
  const [
    { default: ProfilesPage },
    { I18nProvider },
    { ProfileProvider },
    { PageHeaderProvider },
  ] = await Promise.all([
    import("./ProfilesPage"),
    import("@/i18n"),
    import("@/contexts/ProfileProvider"),
    import("@/contexts/PageHeaderProvider"),
  ]);
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () =>
    root.render(
      <I18nProvider>
        <MemoryRouter>
          <ProfileProvider>
            <PageHeaderProvider pluginTabs={[]}>
              <ProfilesPage />
            </PageHeaderProvider>
          </ProfileProvider>
        </MemoryRouter>
      </I18nProvider>,
    ),
  );
  await waitFor(() => Boolean(document.body.textContent?.includes("rmk-intel")));
}

describe("ProfilesPage — shared, profile-scoped model picker", () => {
  it("scopes getModelOptions to the edited profile, not the sidebar profile", async () => {
    await renderPage();
    await openModelPickerFor("rmk-intel");

    expect(pickerProps.current).toBeTruthy();
    await (pickerProps.current!.loader as (o?: unknown) => Promise<unknown>)({});
    expect(apiMocks.getModelOptions).toHaveBeenLastCalledWith(
      expect.objectContaining({ profile: "rmk-intel" }),
    );

    await (pickerProps.current!.loader as (o?: unknown) => Promise<unknown>)({
      refresh: true,
    });
    expect(apiMocks.getModelOptions).toHaveBeenLastCalledWith(
      expect.objectContaining({ profile: "rmk-intel", refresh: true }),
    );
  });

  it("saves through /api/model/set scoped to the edited profile", async () => {
    await renderPage();
    await openModelPickerFor("rmk-intel");

    await (pickerProps.current!.onApply as (a: unknown) => Promise<unknown>)({
      provider: "openrouter",
      model: "anthropic/claude-opus-4.8",
    });

    expect(apiMocks.setModelAssignment).toHaveBeenCalledTimes(1);
    const [body, profile] = apiMocks.setModelAssignment.mock.calls[0];
    expect(profile).toBe("rmk-intel");
    expect(body).toMatchObject({
      scope: "main",
      provider: "openrouter",
      model: "anthropic/claude-opus-4.8",
    });
    expect(apiMocks.setProfileModel).not.toHaveBeenCalled();
  });

  it("targets rmk-review when that card is edited", async () => {
    await renderPage();
    await openModelPickerFor("rmk-review");

    await (pickerProps.current!.loader as (o?: unknown) => Promise<unknown>)({});
    expect(apiMocks.getModelOptions).toHaveBeenLastCalledWith(
      expect.objectContaining({ profile: "rmk-review" }),
    );
  });
});
