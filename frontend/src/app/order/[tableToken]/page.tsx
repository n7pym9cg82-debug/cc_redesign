import { OrderingExperience } from "@/components/ordering-experience";

export default async function TableOrderPage({
  params,
}: { params: Promise<{ tableToken: string }> }) {
  const { tableToken } = await params;
  return <OrderingExperience tableToken={tableToken} />;
}
