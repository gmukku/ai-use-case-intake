import Conversation from "./components/Conversation";

/**
 * A fresh conversation. No session exists until the first message is sent, at which point
 * the URL becomes /s/<id> in place so a refresh lands somewhere real.
 */
export default function Home() {
  return <Conversation initialSessionId={null} />;
}
