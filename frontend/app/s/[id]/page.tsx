import Conversation from "../../components/Conversation";

/**
 * An existing conversation, by id.
 *
 * This one is a Server Component — it does no work in the browser, so it does not need to
 * run there. `params` is a Promise in Next 16; synchronous access was removed.
 */
export default async function Session({ params }: PageProps<"/s/[id]">) {
  const { id } = await params;
  return <Conversation initialSessionId={id} />;
}
