import type { NextConfig } from "next";

const tunnelOrigin = process.env.FRONTEND_DEV_ORIGIN;
const tunnelHost = tunnelOrigin ? new URL(tunnelOrigin).host : null;

const nextConfig: NextConfig = {
  reactStrictMode: true,
  allowedDevOrigins: tunnelHost ? [tunnelHost] : [],
};

export default nextConfig;
