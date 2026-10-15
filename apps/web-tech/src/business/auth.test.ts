import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BrowserCacheLocation, InteractionRequiredAuthError } from "@azure/msal-browser";
import { InsuranceAuth, InteractionNeeded } from "./auth";
import { configExample } from "./testData";

const msal = vi.hoisted(() => ({
  config: vi.fn(), initialize: vi.fn(), handleRedirectPromise: vi.fn(), getActiveAccount: vi.fn(),
  setActiveAccount: vi.fn(), loginPopup: vi.fn(), acquireTokenPopup: vi.fn(), acquireTokenSilent: vi.fn(), clearCache: vi.fn(),
}));
vi.mock("@azure/msal-browser", async importOriginal => {
  const actual = await importOriginal<typeof import("@azure/msal-browser")>();
  return { ...actual, PublicClientApplication: vi.fn(function (config: unknown) { msal.config(config); return msal; }) };
});
const account = { homeAccountId: "home", localAccountId: "local", environment: "login.microsoftonline.com",
  tenantId: configExample.tenantId, username: "synthetic@example.test", name: "Synthetic Customer" };
beforeEach(() => {
  vi.clearAllMocks(); vi.stubGlobal("window", { location: { origin: "http://localhost:5173", pathname: "/" } });
  msal.initialize.mockResolvedValue(undefined); msal.handleRedirectPromise.mockResolvedValue(null);
  msal.getActiveAccount.mockReturnValue(null); msal.setActiveAccount.mockImplementation(a => { msal.getActiveAccount.mockReturnValue(a); });
  msal.loginPopup.mockResolvedValue({ account, accessToken: "token-must-stay-private" });
  msal.acquireTokenPopup.mockResolvedValue({ account, accessToken: "token-must-stay-private" });
  msal.acquireTokenSilent.mockResolvedValue({ account, accessToken: "silent-token" }); msal.clearCache.mockResolvedValue(undefined);
});
afterEach(() => vi.unstubAllGlobals());
describe("Microsoft insurance authentication", () => {
  it("initializes once and handles redirects without automatic login, token calls, or persistent token cache", async () => {
    const auth = new InsuranceAuth(configExample), first = auth.initialize();
    expect(auth.initialize()).toBe(first); await first;
    expect(msal.initialize).toHaveBeenCalledTimes(1); expect(msal.handleRedirectPromise).toHaveBeenCalledTimes(1);
    expect(msal.loginPopup).not.toHaveBeenCalled(); expect(msal.acquireTokenSilent).not.toHaveBeenCalled();
    expect(msal.config).toHaveBeenCalledWith(expect.objectContaining({
      auth: expect.objectContaining({ clientId: configExample.clientId, authority: `https://login.microsoftonline.com/${configExample.tenantId}`, redirectUri: "http://localhost:5173/" }),
      cache: { cacheLocation: BrowserCacheLocation.MemoryStorage, temporaryCacheLocation: BrowserCacheLocation.MemoryStorage, storeAuthStateInCookie: false },
    }));
    expect(auth.snapshot().authenticated).toBe(false);
  });
  it("uses actual explicit MSAL login and silently refreshes only after a signed-in account exists", async () => {
    const auth = new InsuranceAuth(configExample); await auth.initialize();
    await expect(auth.token()).rejects.toBeInstanceOf(InteractionNeeded);
    await auth.signIn();
    expect(msal.loginPopup).toHaveBeenCalledWith({ scopes: [configExample.scope], prompt: "select_account" });
    expect(auth.snapshot()).toMatchObject({ authenticated: true, accountName: "Synthetic Customer", interactionRequired: false });
    expect(await auth.token()).toBe("silent-token");
    expect(msal.acquireTokenSilent).toHaveBeenCalledWith({ scopes: [configExample.scope], account });
    expect(JSON.stringify(auth.snapshot())).not.toContain("token-must-stay-private");
  });
  it("exposes interaction-required and never silently opens a popup or retries the protected action", async () => {
    const auth = new InsuranceAuth(configExample); await auth.initialize(); await auth.signIn();
    msal.acquireTokenSilent.mockRejectedValueOnce(new InteractionRequiredAuthError("interaction_required"));
    await expect(auth.token()).rejects.toBeInstanceOf(InteractionNeeded);
    expect(auth.snapshot().interactionRequired).toBe(true); expect(msal.acquireTokenPopup).not.toHaveBeenCalled();
    await expect(auth.token()).rejects.toBeInstanceOf(InteractionNeeded);
    expect(msal.acquireTokenSilent).toHaveBeenCalledTimes(1);
    await auth.signIn();
    expect(msal.acquireTokenPopup).toHaveBeenCalledWith({ scopes: [configExample.scope], account });
    expect(auth.snapshot().interactionRequired).toBe(false);
  });
  it("does not expose raw MSAL failures, and clears only the local memory session on sign-out", async () => {
    const auth = new InsuranceAuth(configExample); await auth.initialize(); await auth.signIn();
    msal.acquireTokenSilent.mockRejectedValueOnce(new Error("secret error payload"));
    await expect(auth.token()).rejects.toThrow("Unable to refresh");
    expect(JSON.stringify(auth.snapshot())).not.toContain("secret");
    await auth.signOut(); expect(msal.clearCache).toHaveBeenCalledTimes(1);
    expect(auth.snapshot().authenticated).toBe(false); expect(msal.getActiveAccount()).toBeNull();
  });
  it("accepts handled redirect accounts without pretending an identity or server role", async () => {
    msal.handleRedirectPromise.mockResolvedValueOnce({ account });
    const auth = new InsuranceAuth(configExample); await auth.initialize();
    expect(auth.snapshot().authenticated).toBe(true);
    expect(auth.snapshot()).not.toHaveProperty("role");
  });
});
