import { fileURLToPath } from "node:url";
import path from "node:path";
import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export default ({ mode }) => {
  Object.assign(process?.env, loadEnv(mode, __dirname, ""));

  const SERVER_URL = process?.env?.SERVER_URL || "http://localhost:5000";

  return defineConfig({
    root: __dirname,
    server: {
      proxy: {
        "/api": {
          target: SERVER_URL,
          changeOrigin: true,
        },
        "/ws": {
          target: SERVER_URL,
          changeOrigin: true,
          ws: true,
        },
        "/addons": {
          target: SERVER_URL,
          changeOrigin: false,
        },
        "/graphql": {
          target: SERVER_URL,
          changeOrigin: true,
        },
      },
    },
    plugins: [react()],
    base: "",
  });
};
