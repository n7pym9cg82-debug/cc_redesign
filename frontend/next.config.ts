import type { NextConfig } from "next";

const tunnelOrigin = process.env.FRONTEND_DEV_ORIGIN;
const tunnelHost = tunnelOrigin ? new URL(tunnelOrigin).host : null;
const adminApiOrigin = process.env.ADMIN_API_ORIGIN ??
  (process.env.NODE_ENV === "development" ? "http://127.0.0.1:8000" : "");

if (adminApiOrigin) {
  const url = new URL(adminApiOrigin);
  if (url.origin !== adminApiOrigin || (url.protocol !== "https:" &&
      !(url.protocol === "http:" && ["localhost", "127.0.0.1"].includes(url.hostname)))) {
    throw new Error("ADMIN_API_ORIGIN must be an HTTPS origin or a local development origin");
  }
}

const nextConfig: NextConfig = {
  reactStrictMode: true,
  allowedDevOrigins: tunnelHost ? [tunnelHost] : [],
  async rewrites() {
    return adminApiOrigin ? [{
      source: "/api/admin/:path*",
      destination: `${adminApiOrigin}/api/admin/:path*`,
    }] : [];
  },
};

export default nextConfig;
