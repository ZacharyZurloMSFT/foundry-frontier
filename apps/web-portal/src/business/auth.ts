import { BrowserCacheLocation, InteractionRequiredAuthError, PublicClientApplication } from "@azure/msal-browser";
import { BusinessConfig } from "./config";

export interface AuthState {
  ready: boolean; busy: boolean; authenticated: boolean; accountName: string;
  interactionRequired: boolean; error: string | null; revision: number;
}
export class InteractionNeeded extends Error {
  constructor() { super("Your Microsoft session needs attention. Use Continue with Microsoft, then explicitly retry your action."); }
}
export class InsuranceAuth {
  private readonly client: PublicClientApplication;
  private initialization: Promise<void> | null = null;
  private listeners = new Set<() => void>();
  private state: AuthState = { ready: false, busy: false, authenticated: false, accountName: "", interactionRequired: false, error: null, revision: 0 };
  constructor(private readonly config: BusinessConfig) {
    this.client = new PublicClientApplication({
      auth: {
        clientId: config.clientId, authority: `https://login.microsoftonline.com/${config.tenantId}`,
        redirectUri: `${window.location.origin}${window.location.pathname}`,
        navigateToLoginRequestUrl: false,
      },
      cache: {
        cacheLocation: BrowserCacheLocation.MemoryStorage,
        temporaryCacheLocation: BrowserCacheLocation.MemoryStorage,
        storeAuthStateInCookie: false,
      },
      system: { loggerOptions: { piiLoggingEnabled: false, loggerCallback: () => {} } },
    });
  }
  snapshot = (): AuthState => this.state;
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => this.listeners.delete(listener); };
  private update(patch: Partial<AuthState>) { this.state = { ...this.state, ...patch }; this.listeners.forEach(l => l()); }
  initialize(): Promise<void> {
    if (!this.initialization) this.initialization = this.start();
    return this.initialization;
  }
  private async start() {
    try {
      await this.client.initialize();
      const redirect = await this.client.handleRedirectPromise();
      const account = redirect?.account ?? this.client.getActiveAccount();
      if (account) this.client.setActiveAccount(account);
      this.update({ ready: true, authenticated: !!account, accountName: account?.name || account?.username || "" });
    } catch {
      this.update({ ready: true, error: "Microsoft sign-in could not initialize. Reload this page or contact the portal administrator." });
    }
  }
  // Popup flows use authorization code + PKCE; nothing redirects without a user action.
  async signIn() {
    if (!this.state.ready || this.state.busy) return;
    this.update({ busy: true, error: null });
    try {
      const account = this.client.getActiveAccount();
      const response = account
        ? await this.client.acquireTokenPopup({ scopes: [this.config.scope], account })
        : await this.client.loginPopup({ scopes: [this.config.scope], prompt: "select_account" });
      if (!response.account) throw new Error("No account");
      this.client.setActiveAccount(response.account);
      this.update({ authenticated: true, accountName: response.account.name || response.account.username,
        interactionRequired: false, revision: this.state.revision + 1 });
    } catch {
      this.update({ error: "Microsoft sign-in was not completed. Allow the sign-in popup and try again; no claim action was submitted." });
    } finally { this.update({ busy: false }); }
  }
  requireInteraction = () => { this.update({ interactionRequired: true }); };
  token = async (): Promise<string> => {
    await this.initialize();
    const account = this.client.getActiveAccount();
    if (!account || this.state.interactionRequired) { this.requireInteraction(); throw new InteractionNeeded(); }
    try {
      const result = await this.client.acquireTokenSilent({ scopes: [this.config.scope], account });
      if (!result.accessToken) throw new Error("Missing token");
      return result.accessToken;
    } catch (error) {
      if (error instanceof InteractionRequiredAuthError) { this.requireInteraction(); throw new InteractionNeeded(); }
      throw new Error("Unable to refresh your Microsoft access token. No request was sent. Try again or sign out and sign in.");
    }
  };
  async signOut() {
    if (this.state.busy) return;
    this.update({ busy: true });
    try {
      // Local clear removes memory-only tokens without navigating to an identity provider.
      await this.client.clearCache();
      this.client.setActiveAccount(null);
      this.update({ authenticated: false, accountName: "", interactionRequired: false, error: null, revision: this.state.revision + 1 });
    } catch { this.update({ error: "Local sign-out did not complete. Close this browser tab to clear this memory-only session." }); }
    finally { this.update({ busy: false }); }
  }
}
let instance: InsuranceAuth | undefined;
export function getInsuranceAuth(config: BusinessConfig): InsuranceAuth {
  instance ??= new InsuranceAuth(config);
  return instance;
}
