"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";
import { adminApi, AdminApiError } from "@/lib/admin-api";

export function AdminShell({ children }: { children: ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [username, setUsername] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [loggingOut, setLoggingOut] = useState(false);

  useEffect(() => {
    let active = true;
    adminApi.me().then((user) => { if (active) setUsername(user.username); }).catch((failure) => {
      if (!active) return;
      if (failure instanceof AdminApiError && failure.status === 401) {
        router.replace("/admin/login");
      } else {
        setError("Admin authentication is unavailable. Please refresh to try again.");
      }
    });
    return () => { active = false; };
  }, [router]);

  async function logout() {
    setLoggingOut(true);
    setError("");
    try {
      await adminApi.logout();
      router.replace("/admin/login");
    } catch {
      setError("Could not sign out. Please try again.");
      setLoggingOut(false);
    }
  }

  if (!username) return <main className="admin-loading" role="status">
    {error || "Checking staff access…"}
  </main>;

  return <div className="admin-app">
    <header className="admin-header">
      <div className="admin-header-inner">
        <Link className="admin-brand" href="/admin/tables">Chennai Café <span>Admin</span></Link>
        <nav aria-label="Admin navigation">
          <Link href="/admin/tables" aria-current={pathname === "/admin/tables" ? "page" : undefined}>Tables</Link>
          <Link href="/admin/history" aria-current={pathname === "/admin/history" ? "page" : undefined}>History</Link>
          <Link href="/">Public Site</Link>
        </nav>
        <button className="admin-logout" type="button" onClick={logout} disabled={loggingOut}>Logout</button>
      </div>
    </header>
    <main className="admin-main">
      {error && <p className="admin-alert" role="alert">{error}</p>}
      {children}
    </main>
  </div>;
}
