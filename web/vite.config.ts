import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => {
  const fileEnv = loadEnv(mode, process.cwd(), "");
  const serviceUrl = process.env.DECISION_INBOX_DEV_SERVICE_URL
    || fileEnv.DECISION_INBOX_DEV_SERVICE_URL
    || "http://127.0.0.1:8080";
  const ownerLogin = process.env.VITE_TAILSCALE_OWNER_LOGIN
    || process.env.DECISION_INBOX_TAILSCALE_OWNER_LOGIN
    || fileEnv.VITE_TAILSCALE_OWNER_LOGIN
    || fileEnv.DECISION_INBOX_TAILSCALE_OWNER_LOGIN;

  return {
    server: {
      host: "127.0.0.1",
      port: 5173,
      proxy: {
        "/api": {
          target: serviceUrl,
          ws: true,
          configure(proxy) {
            if (ownerLogin) proxy.on("proxyReq", request => request.setHeader("Tailscale-User-Login", ownerLogin));
          },
        },
        "/healthz": { target: serviceUrl },
      },
    },
    test: { environment: "node" },
  };
});
