import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Chennai Café | Table ordering",
  robots: { index: false, follow: false },
};

export default function TableOrderLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return children;
}
