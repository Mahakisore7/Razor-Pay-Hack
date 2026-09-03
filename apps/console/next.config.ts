import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // Standalone output traces the minimal set of files a production server
  // needs and copies them out of node_modules -- the Docker runtime stage
  // (T0.5) ships that instead of the full workspace + node_modules.
  output: "standalone",
  eslint: {
    // Linting is a separate, explicit CI step (T0.7) so a build failure and a
    // lint failure are distinguishable in CI output rather than conflated.
    ignoreDuringBuilds: true,
  },
};

export default nextConfig;
