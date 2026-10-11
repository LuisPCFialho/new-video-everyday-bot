// Disparador externo do workflow publish.yml (Cloudflare Worker com Cron Trigger).
//
// O agendamento nativo do GitHub Actions descarta a maior parte dos disparos deste
// repositório; o cron da Cloudflare é pontual. A Worker só pede ao GitHub para correr o
// workflow em modo "scheduled" — é o bot (reels_bot.slots) que decide se há slot.
//
// Configuração no painel da Cloudflare (Workers & Pages → reels-trigger):
//   - Settings → Variables and Secrets → Secret  GH_TOKEN  = token do GitHub
//     (fine-grained, só este repositório, permissão "Actions: Read and write")
//   - Settings → Triggers → Cron Triggers →  13,43 11-15,18-23 * * *   (UTC, igual ao workflow)

const DISPATCH_URL =
  "https://api.github.com/repos/LuisPCFialho/new-video-everyday-bot/actions/workflows/publish.yml/dispatches";

async function dispatch(env) {
  const res = await fetch(DISPATCH_URL, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GH_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "reels-trigger",
    },
    body: JSON.stringify({ ref: "main", inputs: { scheduled: "true" } }),
  });
  if (res.status !== 204) {
    throw new Error(`GitHub respondeu ${res.status}: ${await res.text()}`);
  }
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(dispatch(env));
  },
};
