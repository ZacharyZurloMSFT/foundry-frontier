import { describe, expect, it } from "vitest";
import dockerfile from "../../Dockerfile?raw";
import localDockerfile from "../../Dockerfile.local?raw";

const files: Record<string, string> = { Dockerfile: dockerfile, "Dockerfile.local": localDockerfile };
describe("insurance Docker build-time configuration", () => {
  it.each(["Dockerfile", "Dockerfile.local"])("%s declares disabled/blank business arguments and passes them into Vite before building", file => {
    const content = files[file].replace(/\r\n/g, "\n");
    const defaults = {
      VITE_INSURANCE_BUSINESS_ENABLED: "false", VITE_INSURANCE_AUTH_TENANT_ID: "",
      VITE_INSURANCE_AUTH_CLIENT_ID: "", VITE_INSURANCE_AUTH_SCOPE: "", VITE_INSURANCE_RUN_ID: "",
    };
    for (const [key, value] of Object.entries(defaults)) {
      expect(content).toContain(`ARG ${key}=${value}\n`);
      expect(content).toContain(`${key}=\${${key}}`);
      expect(content.indexOf(`ARG ${key}=`)).toBeLessThan(content.indexOf("npm run build"));
    }
    expect(content).toContain("ARG VITE_API_BASE_URL=http://localhost:8000");
    expect(content).toContain("COPY --from=build /src/dist /usr/share/nginx/html");
  });
  it("keeps production's prepackaged default and only rebuilds for an explicit opt-in", () => {
    const content = dockerfile;
    expect(content).toContain('if [ "$VITE_INSURANCE_BUSINESS_ENABLED" = "true" ]; then');
    expect(content).toContain("COPY . .");
    expect(content).toContain("npm ci --no-audit --no-fund && npm run build");
  });
});
