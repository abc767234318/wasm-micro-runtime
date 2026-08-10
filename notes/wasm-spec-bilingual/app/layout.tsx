import type { Metadata } from "next";
import { headers } from "next/headers";
import "./globals.css";

export async function generateMetadata(): Promise<Metadata> {
  const requestHeaders = await headers();
  const host = requestHeaders.get("x-forwarded-host") ?? requestHeaders.get("host") ?? "localhost";
  const protocol = requestHeaders.get("x-forwarded-proto") ?? (host.includes("localhost") ? "http" : "https");
  const metadataBase = new URL(`${protocol}://${host}`);
  return {
    metadataBase,
    title: "Wasm Spec 中英对照 | WebAssembly 3.0",
    description: "WebAssembly Core Specification 3.0 官方英文与中文对照文本双栏版。",
    icons: { icon: "/favicon.svg", shortcut: "/favicon.svg" },
    openGraph: {
      title: "Wasm Spec 中英对照",
      description: "English × 中文 · WebAssembly 3.0",
      type: "website",
      images: [{ url: "/spec/_bilingual/og.png", width: 1731, height: 909 }],
    },
    twitter: {
      card: "summary_large_image",
      title: "Wasm Spec 中英对照",
      description: "English × 中文 · WebAssembly 3.0",
      images: ["/spec/_bilingual/og.png"],
    },
  };
}

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="zh-CN"><body>{children}</body></html>;
}
