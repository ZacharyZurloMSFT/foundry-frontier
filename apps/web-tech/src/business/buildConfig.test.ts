import { describe, expect, it } from "vitest";
import dockerfile from "../../Dockerfile?raw";
import localDockerfile from "../../Dockerfile.local?raw";

const files: Record<string, string> = { Dockerfile: dockerfile, "Dockerfile.local": localDockerfile };
describe("insurance Docker build-time configuration", () => {
  it("Dockerfile.local declares blank identity arguments and passes them into Vite before building", () => {
    const content = localDockerfile.replace(/\r\n/g, "\n");
    const defaults = {
      VITE_INSURANCE_AUTH_TENANT_ID: "", VITE_INSURANCE_AUTH_CLIENT_ID: "",
      VITE_INSURANCE_AUTH_SCOPE: "", VITE_INSURANCE_RUN_ID: "",
    };
    for (const [key, value] of Object.entries(defaults)) {
      expect(content).toContain(`ARG ${key}=${value}\n`);
      expect(content).toContain(`${key}=\${${key}}`);
      expect(content.indexOf(`ARG ${key}=`)).toBeLessThan(content.indexOf("npm run build"));
    }
    expect(content).toContain("ARG VITE_API_BASE_URL=http://localhost:8000");
    expect(content).toContain("COPY --from=build /src/dist /usr/share/nginx/html");
  });
  it("keeps production on the prebuilt dist and never rebuilds the SPA in the image", () => {
    const content = dockerfile.replace(/\r\n/g, "\n");
    expect(content).toContain("COPY dist /usr/share/nginx/html");
    expect(content).not.toMatch(/^RUN /m);
  });
  it.each(["Dockerfile", "Dockerfile.local"])("%s has no business/legacy shell switch", file => {
    expect(files[file]).not.toContain("VITE_INSURANCE_BUSINESS_ENABLED");
  });
});
