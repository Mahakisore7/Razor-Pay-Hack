import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  eslint: {
    // Linting is a separate, explicit CI step (T0.7) so a build failure and a
    // lint failure are distinguishable in CI output rather than conflated.
    ignoreDuringBuilds: true,
  },
};

export default nextConfig;
