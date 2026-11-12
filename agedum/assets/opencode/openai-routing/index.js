const UPSTREAM_HEADER = "X-Agedum-OpenAI-Upstream";

export default {
  id: "agedum.openai-routing",
  async setup(ctx) {
    const proxy = new URL(ctx.options.baseURL);
    await ctx.session.hook("http.request", async event => {
      const url = new URL(event.request.url);
      if (event.model.providerID !== "openai" || url.origin !== proxy.origin ||
          !url.pathname.startsWith("/oc/openai/")) return;

      // OpenCode owns credential refresh and account selection; inspect only its method.
      const connection = await ctx.integration.connection.active("openai");
      const credential = connection ? await ctx.integration.connection.resolve(connection) : undefined;
      const legacyOAuth = credential?.type === "oauth" &&
        credential.methodID !== "chatgpt-token-sharing";
      event.request.headers.set(UPSTREAM_HEADER, legacyOAuth ? "codex" : "api");
    });
  },
};
