import { expect, test } from "@playwright/test";

import { ApiStub, frame, turn } from "./stub-api";

/**
 * The requester view, in a real browser.
 *
 * Every test here covers something the unit tests structurally cannot reach: the SSE reader
 * against an actual `ReadableStream`, React state across a whole turn, and layout — one of
 * these regressions was a button rendered correctly and covered by a sticky bar, which no
 * assertion about component state would have found.
 *
 * Three of them are regression tests for bugs this project actually shipped. They are marked.
 */

const SESSION = "e2e-session";

test.describe("taking a turn", () => {
  test("streams the reply and re-enables the box afterwards", async ({ page }) => {
    const api = new ApiStub();
    api.setScript(turn("Two coordinators, and it takes about twenty minutes each."));
    await api.install(page);

    await page.goto(`/s/${SESSION}`);
    const box = page.getByLabel("Your message");
    await expect(box).toBeEnabled();

    await box.fill("We check onboarding packets by hand.");
    await page.getByRole("button", { name: /send/i }).click();

    await expect(page.getByText(/Two coordinators/)).toBeVisible();
    // The box must come back, or the conversation is over after one message.
    await expect(box).toBeEnabled();
    // Clearing matters: a composer that keeps the sent text invites sending it twice.
    await expect(box).toHaveValue("");
  });

  test("shows the requester's own message immediately, before any reply arrives", async ({
    page,
  }) => {
    const api = new ApiStub();
    // A slow server: the turn does not answer for a while. What the requester typed must be
    // on screen the whole time, or it looks like it was lost.
    api.setScript(turn("Thanks — who else depends on that?"), 800);
    await api.install(page);

    await page.goto(`/s/${SESSION}`);
    await page.getByLabel("Your message").fill("Packets arrive as scanned PDFs.");
    await page.getByRole("button", { name: /send/i }).click();

    await expect(page.getByText("Packets arrive as scanned PDFs.")).toBeVisible();
    await expect(page.getByText(/Thinking/)).toBeVisible();
    await expect(page.getByText(/who else depends/)).toBeVisible();
  });

  test("assembles a frame that arrives split across chunks", async ({ page }) => {
    // The reason `streamTurn` buffers at all: SSE frame boundaries and network chunk
    // boundaries are unrelated, so a frame can be cut anywhere — including mid-JSON. Split
    // inside the data line, inside the terminator, and between two frames.
    const api = new ApiStub();
    const whole = turn("Salesforce and a shared inbox.").join("");
    const cut1 = Math.floor(whole.length * 0.3);
    const cut2 = Math.floor(whole.length * 0.6);
    api.setScript([whole.slice(0, cut1), whole.slice(cut1, cut2), whole.slice(cut2)]);
    await api.install(page);

    await page.goto(`/s/${SESSION}`);
    await page.getByLabel("Your message").fill("Which systems?");
    await page.getByRole("button", { name: /send/i }).click();

    await expect(page.getByText("Salesforce and a shared inbox.")).toBeVisible();
  });

  test("concatenates several text frames into one message", async ({ page }) => {
    const api = new ApiStub();
    api.setScript([
      frame("text", { text: "Got it. " }),
      frame("activity"),
      frame("text", { text: "How often does that happen?" }),
      frame("done", { turn: 1, is_complete: false, ready_for_review: false, error: null }),
    ]);
    await api.install(page);

    await page.goto(`/s/${SESSION}`);
    await page.getByLabel("Your message").fill("Weekly, mostly.");
    await page.getByRole("button", { name: /send/i }).click();

    await expect(page.getByText("Got it. How often does that happen?")).toBeVisible();
  });
});

test.describe("when things go wrong", () => {
  test("shows an error and keeps what was already said", async ({ page }) => {
    const api = new ApiStub();
    api.setScript([
      frame("text", { text: "Let me check that." }),
      frame("error", { message: "The model stopped responding." }),
    ]);
    await api.install(page);

    await page.goto(`/s/${SESSION}`);
    await page.getByLabel("Your message").fill("Anything else?");
    await page.getByRole("button", { name: /send/i }).click();

    // Scoped to `main`: Next injects its own route announcer with role="alert" at body
    // level, so an unscoped getByRole("alert") is ambiguous and fails strict mode.
    await expect(page.getByRole("main").getByRole("alert")).toContainText(
      "The model stopped responding.",
    );
    // Losing the transcript on an error would be worse than the error.
    await expect(page.getByText("Anything else?")).toBeVisible();
    await expect(page.getByLabel("Your message")).toBeEnabled();
  });

  test("a stream that stops mid-frame does not hang the composer", async ({ page }) => {
    // No `done`, and the last frame is truncated: the server died halfway. The turn has to
    // end anyway, or the requester is stuck with a disabled box and no way back.
    const api = new ApiStub();
    api.setScript([frame("text", { text: "Partial answer" }), 'event: text\ndata: {"text": "cu']);
    await api.install(page);

    await page.goto(`/s/${SESSION}`);
    await page.getByLabel("Your message").fill("Go on.");
    await page.getByRole("button", { name: /send/i }).click();

    await expect(page.getByLabel("Your message")).toBeEnabled({ timeout: 15_000 });
  });
});

test.describe("restoring a conversation", () => {
  test("a reload brings the whole transcript back", async ({ page }) => {
    // Regression: `_restore` did not replay turn results, so saving after a resume wrote one
    // turn over ten. Nine messages vanished, and only reloading the page showed it.
    const api = new ApiStub({ turn: 3 });
    api.setTranscript([
      { turn: 1, role: "you", text: "We check onboarding packets by hand." },
      { turn: 1, role: "agent", text: "Who does that today?" },
      { turn: 2, role: "you", text: "Two HR coordinators." },
      { turn: 2, role: "agent", text: "And how often?" },
      { turn: 3, role: "you", text: "About thirty a month." },
      { turn: 3, role: "agent", text: "Which systems does it touch?" },
    ]);
    await api.install(page);

    await page.goto(`/s/${SESSION}`);
    for (const text of [
      "We check onboarding packets by hand.",
      "Two HR coordinators.",
      "About thirty a month.",
      "Which systems does it touch?",
    ]) {
      await expect(page.getByText(text)).toBeVisible();
    }
  });
});

test.describe("after review", () => {
  test("a finished conversation shows the outcome and closes the box", async ({ page }) => {
    const api = new ApiStub({
      is_complete: true,
      ready_for_review: true,
      review_status: "approved",
      outcome: "Approved. A prototype is being put together.",
    });
    await api.install(page);

    await page.goto(`/s/${SESSION}`);
    await expect(page.getByText(/Approved\. A prototype/)).toBeVisible();
    await expect(page.getByLabel("Your message")).toBeDisabled();
  });

  test("a send-back lets the requester answer again", async ({ page }) => {
    // Regression, and the sharpest one on this surface. `review_status` stays "sent_back"
    // after the reviewer's question is delivered, so using it to decide *both* "is there a
    // question waiting?" and "may they type?" first offered the question forever, and then,
    // once fixed with a single swapped predicate, locked the requester out of answering it.
    // Two questions, two booleans — and this asserts both halves.
    const api = new ApiStub({
      is_complete: true,
      ready_for_review: true,
      review_status: "sent_back",
      can_reopen: true,
      outcome: "A reviewer has a question about this.",
    });
    api.setScript(turn("The reviewer asks: which systems are written into?", { turn: 4 }));
    await api.install(page);

    await page.goto(`/s/${SESSION}`);
    const reopen = page.getByRole("button", { name: /continue the conversation/i });
    await expect(reopen).toBeVisible();

    // The button must be clickable, not merely present: a sticky composer once floated over
    // it and hid it completely. Playwright's actionability check is the assertion.
    await reopen.click();
    await expect(page.getByText(/which systems are written into/)).toBeVisible();

    // The half that regressed: after the question is delivered the box must be usable.
    await expect(page.getByLabel("Your message")).toBeEnabled();
  });

  test("once the question has been delivered it is not offered again", async ({ page }) => {
    // The other half of the same bug, and the state that made it visible: `review_status`
    // stays "sent_back" after delivery, so anything deriving "is there a question waiting?"
    // from it offers the same question forever — and a reload would ask the requester the
    // same thing twice. The server decides with `can_reopen`; this asserts the client honours
    // it rather than re-deriving it from the status.
    //
    // Deliberately a separate test rather than a second assertion on the one above: the
    // client refreshes status on a send, a reopen, or the poll that runs while waiting, so
    // mutating the stub mid-test asserts nothing except that React kept its state.
    const api = new ApiStub({
      is_complete: true,
      ready_for_review: true,
      review_status: "sent_back",
      can_reopen: false,
      outcome: "A reviewer has a question about this.",
    });
    await api.install(page);

    await page.goto(`/s/${SESSION}`);
    await expect(page.getByText(/A reviewer has a question/)).toBeVisible();
    await expect(
      page.getByRole("button", { name: /continue the conversation/i }),
    ).toHaveCount(0);
    // Still their conversation: sent_back means they may answer.
    await expect(page.getByLabel("Your message")).toBeEnabled();
  });

  test("the reopen button stays clear of the sticky composer at the foot of a long page", async ({
    page,
  }) => {
    // Geometry, and the only kind of bug in this file invisible to every other test we have:
    // the button rendered correctly and was covered by the composer floating over it. Cost
    // two "fixes" to the wrong thing before anything was measured.
    //
    // The long transcript is load-bearing. The first version of this test used a short one,
    // and with nothing to scroll the composer sits at the foot of the viewport well below the
    // button — so it passed against a layout with the spacer deliberately removed. Verified
    // by mutation: `pb-28` -> `pb-0` survived until the page actually scrolled.
    const api = new ApiStub({
      is_complete: true,
      ready_for_review: true,
      review_status: "sent_back",
      can_reopen: true,
      outcome: "A reviewer has a question about this.",
    });
    api.setTranscript(
      Array.from({ length: 24 }, (_, i) => ({
        turn: Math.floor(i / 2) + 1,
        role: i % 2 === 0 ? ("you" as const) : ("agent" as const),
        text: `Message ${i + 1}. Enough text here to make the conversation taller than the window.`,
      })),
    );
    await api.install(page);

    await page.goto(`/s/${SESSION}`);
    const reopen = page.getByRole("button", { name: /continue the conversation/i });
    await expect(reopen).toBeVisible();

    const scrolls = await page.evaluate(() => {
      window.scrollTo(0, document.body.scrollHeight);
      return document.body.scrollHeight > window.innerHeight;
    });
    expect(scrolls, "the page must actually scroll or this test proves nothing").toBe(true);

    // Hit-testing, not arithmetic. An earlier version compared the button's bottom against
    // the *input's* bounding box, which is the wrong element: the composer bar's margin and
    // padding start some 50px above its input, so the button can sit behind the bar while the
    // numbers still read as clear. Measured rather than guessed — with both layout
    // protections removed the button ended at 651px and the input began at 662px.
    //
    // `elementFromPoint` asks the browser the question that matters: if a click landed in the
    // middle of this button, what would receive it?
    //
    // UNPROVEN, and flagged as such. Three mutations were tried — removing the `pb-28`
    // spacer, taking the composer out of flow, and both together — and this assertion passed
    // against all three, because the button's centre stays clear even then (centre at 633px,
    // bar top around 647px; only its last few pixels are covered). So this guards the right
    // invariant and has never been shown to fail. Treat it as a tripwire for a future layout
    // change, not as a regression test for the bug that prompted it.
    const hit = await page.evaluate(() => {
      const button = [...document.querySelectorAll("button")].find((b) =>
        /continue the conversation/i.test(b.textContent ?? ""),
      );
      if (!button) return { found: false, reaches: false, covering: null as string | null };
      const r = button.getBoundingClientRect();
      const top = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
      return {
        found: true,
        reaches: !!top && button.contains(top),
        covering: top ? `${top.tagName}.${top.className}`.slice(0, 120) : null,
      };
    });

    expect(hit.found, "the reopen button should be in the document").toBe(true);
    expect(hit.reaches, `a click in the middle of the button would hit ${hit.covering}`).toBe(true);
  });
});

test.describe("the composer", () => {
  test("will not send an empty or whitespace-only message", async ({ page }) => {
    const api = new ApiStub();
    await api.install(page);
    await page.goto(`/s/${SESSION}`);

    const send = page.getByRole("button", { name: /send/i });
    await expect(send).toBeDisabled();

    await page.getByLabel("Your message").fill("   ");
    await expect(send).toBeDisabled();
    // Nothing reached the server: a blank turn still costs a model call.
    expect(api.calls.filter((c) => c.startsWith("POST"))).toHaveLength(0);
  });
});
