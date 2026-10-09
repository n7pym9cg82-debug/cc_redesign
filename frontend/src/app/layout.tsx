import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Chennai Café | Good Food Brings People Together",
  description: "Explore Chennai Café, our menu preview, and information about visiting.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}
