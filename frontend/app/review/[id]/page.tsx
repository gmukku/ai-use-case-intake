import ReviewDetail from "../../components/ReviewDetail";

export default async function ReviewPage({ params }: PageProps<"/review/[id]">) {
  const { id } = await params;
  return <ReviewDetail sessionId={id} />;
}
