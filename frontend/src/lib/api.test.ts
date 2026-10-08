import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, customerApi } from "./api";

const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => {
  void _input;
  void _init;
  return {
    ok: true,
    json: async () => ({ table: "T12", display_name: "Table 12", valid: true }),
  };
});

function requestHeaders(): Headers {
  const init = fetchMock.mock.calls.at(-1)?.[1] as RequestInit | undefined;
  return new Headers(init?.headers);
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal("fetch", fetchMock);
  vi.stubEnv("NODE_ENV", "development");
  vi.stubEnv("NEXT_PUBLIC_API_BASE_URL", "https://test.ngrok-free.dev");
  vi.stubEnv("NEXT_PUBLIC_NGROK_BYPASS_WARNING", "true");
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe("development ngrok API headers", () => {
  it("recognizes the safe confirmation-pending code without exposing provider details", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 503,
      json: async () => ({ detail: "Order sent; confirmation pending", code: "ORDER_CONFIRMATION_PENDING" }) })));
    await expect(customerApi.placeOrder("session", "token", [
      { item_id: "BIRYANI", quantity: 1 },
    ], "same-key-123")).rejects.toMatchObject({
      status: 503, code: "ORDER_CONFIRMATION_PENDING",
    } satisfies Partial<ApiError>);
  });
  it("adds the bypass header to typed API fetches when explicitly enabled", async () => {
    await customerApi.resolveTable("signed-token");
    expect(requestHeaders().get("ngrok-skip-browser-warning")).toBe("true");
    expect(requestHeaders().get("Content-Type")).toBe("application/json");
    expect(fetchMock.mock.calls[0][0]).toBe(
      "https://test.ngrok-free.dev/api/public/table/signed-token");
  });

  it("does not add the bypass when the flag is disabled", async () => {
    vi.stubEnv("NEXT_PUBLIC_NGROK_BYPASS_WARNING", "false");
    await customerApi.menu();
    expect(requestHeaders().has("ngrok-skip-browser-warning")).toBe(false);
  });

  it("does not add the bypass in production or for a non-ngrok host", async () => {
    vi.stubEnv("NODE_ENV", "production");
    await customerApi.menu();
    expect(requestHeaders().has("ngrok-skip-browser-warning")).toBe(false);
    vi.stubEnv("NODE_ENV", "development");
    vi.stubEnv("NEXT_PUBLIC_API_BASE_URL", "https://api.example.com");
    await customerApi.menu();
    expect(requestHeaders().has("ngrok-skip-browser-warning")).toBe(false);
  });

  it("preserves Content-Type and Idempotency-Key on order submission", async () => {
    await customerApi.placeOrder("session", "signed-token", [
      { item_id: "BIRYANI", quantity: 1 },
    ], "unique-key-123");
    const headers = requestHeaders();
    expect(headers.get("Content-Type")).toBe("application/json");
    expect(headers.get("Idempotency-Key")).toBe("unique-key-123");
    expect(headers.get("ngrok-skip-browser-warning")).toBe("true");
  });
});
