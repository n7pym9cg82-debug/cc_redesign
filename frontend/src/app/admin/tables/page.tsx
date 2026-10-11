import { ActiveTables } from "@/components/admin/admin-pages";

export default async function AdminTablesPage({ searchParams }: {
  searchParams: Promise<{ view?: string }>;
}) {
  const { view } = await searchParams;
  return <ActiveTables initialView={view === "pending" ? "pending" : "tables"} />;
}
