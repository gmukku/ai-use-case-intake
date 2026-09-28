import TraceView from "../../components/TraceView";

export default async function TracePage({ params }: PageProps<"/admin/[id]">) {
  const { id } = await params;
  return <TraceView sessionId={id} />;
}
