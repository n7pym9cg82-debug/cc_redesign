import { afterEach, describe, expect, it, vi } from "vitest";
import { adminApi, AdminApiError } from "./admin-api";

afterEach(() => vi.unstubAllGlobals());

describe("admin API client", () => {
  it("uses same-origin credentials and the protected admin path", async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ username: "operator" }) });
    vi.stubGlobal("fetch", fetcher);
    await adminApi.login("operator", "synthetic-password");
    expect(fetcher).toHaveBeenCalledWith("/api/admin/login", expect.objectContaining({
      method: "POST", credentials: "same-origin", cache: "no-store",
      body: JSON.stringify({ username: "operator", password: "synthetic-password" }),
    }));
    await adminApi.me();
    expect(fetcher).toHaveBeenCalledWith("/api/admin/me", expect.objectContaining({
      credentials: "same-origin",
    }));
  });

  it("does not treat an unauthenticated response as table data", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 401 }));
    await expect(adminApi.active()).rejects.toEqual(
      expect.objectContaining({ status: 401, message: "Please sign in to continue." }),
    );
    await expect(adminApi.close("a".repeat(32))).rejects.toBeInstanceOf(AdminApiError);
  });

  it("sends a force-close reason only to the protected admin endpoint", async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ status: "closed" }) });
    vi.stubGlobal("fetch", fetcher);
    await adminApi.forceClose("a".repeat(32), "Clover unavailable");
    expect(fetcher).toHaveBeenCalledWith(
      `/api/admin/table-sessions/${"a".repeat(32)}/force-close`,
      expect.objectContaining({ method: "POST", credentials: "same-origin",
        body: JSON.stringify({ reason: "Clover unavailable" }),
        headers: expect.objectContaining({ "Content-Type": "application/json" }) }),
    );
  });
});
