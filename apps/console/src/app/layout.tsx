import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Recoup — Ops Console",
  description: "Revenue recovery control plane operations console",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body className="min-h-screen antialiased">{children}</body>
    </html>
  );
}
