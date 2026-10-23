const CONTINUATION_REMINDER = "If this task resumes after a temporary provider-limit wait, continue the original task from its recorded state. Do not treat an earlier usage-limit error as a permanent block or as a review result. Reconcile unfinished worker work before retrying it; do not repeat completed side effects. This instruction does not authorize changing provider/model or bypassing permissions/review.";

export default {
  id: "agedum.recovery-continuation",
  async setup(ctx) {
    await ctx.session.hook("context", (event) => {
      event.system.push({ type: "text", text: CONTINUATION_REMINDER });
    });
  },
};
