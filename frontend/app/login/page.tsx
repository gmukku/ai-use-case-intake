import { Suspense } from "react";

import LoginForm from "../components/LoginForm";

export default function Login() {
  // useSearchParams needs a Suspense boundary above it during prerender.
  return (
    <Suspense fallback={null}>
      <LoginForm />
    </Suspense>
  );
}
