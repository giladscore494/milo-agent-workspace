/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'standalone',
  // Lets the isolated E2E stack run two dev servers side by side without
  // sharing a build directory; production builds keep the default.
  distDir: process.env.NEXT_DIST_DIR || '.next',
};
export default nextConfig;
