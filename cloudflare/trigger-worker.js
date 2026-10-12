// Disparador externo do workflow publish.yml (Cloudflare Worker com Cron Trigger).
//
// O agendamento nativo do GitHub Actions descarta a maior parte dos disparos deste
// repositório; o cron da Cloudflare é pontual. A Worker só pede ao GitHub para correr o
// workflow em modo "scheduled" — é o bot (reels_bot.slots) que decide se há slot.
//
// Configuração no painel da Cloudflare (Workers & Pages → reels-trigger):
//   - Settings → Variables and Secrets → Secret  GH_TOKEN  = token do GitHub
//     (fine-grained, só este repositório, permissão "Actions: Read and write")
//   - Settings → Trigger events → Cron Triggers →  13,43 11-15,18-23 * * *   (UTC, igual ao workflow)
//
// Teste: abrir o URL da Worker (https://reels-trigger.<subdomínio>.workers.dev) mostra se o
// token chega ao workflow. Só lê; não dispara nada.

const WORKFLOW_URL =
  "https://api.github.com/repos/LuisPCFialho/new-video-everyday-bot/actions/workflows/publish.yml";

function headers(env) {
  return {
    Authorization: `Bearer ${env.GH_TOKEN}`,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "reels-trigger",
  };
}

async function dispatch(env) {
  const res = await fetch(`${WORKFLOW_URL}/dispatches`, {
    method: "POST",
    headers: headers(env),
    body: JSON.stringify({ ref: "main", inputs: { scheduled: "true" } }),
  });
  if (res.status !== 204) {
    throw new Error(`GitHub respondeu ${res.status}: ${await res.text()}`);
  }
}

async function selfTest(env) {
  if (!env.GH_TOKEN) return "ERRO: falta o secret GH_TOKEN (Settings → Variables and Secrets).";
  const res = await fetch(WORKFLOW_URL, { headers: headers(env) });
  if (res.ok) {
    const wf = await res.json();
    return `OK: o token chega ao workflow "${wf.name}" (estado: ${wf.state}).\n` +
      "Confirma só que o token tem 'Actions: Read and write' (não 'Read-only').";
  }
  const hints = {
    401: "token inválido ou mal colado no GH_TOKEN",
    403: "o token não tem permissão de Actions",
    404: "o token não inclui o repositório new-video-everyday-bot",
  };
  return `ERRO ${res.status}: ${hints[res.status] || "ver detalhe"}\n${await res.text()}`;
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(dispatch(env));
  },
  async fetch(request, env) {
    return new Response(await selfTest(env), {
      headers: { "content-type": "text/plain; charset=utf-8" },
    });
  },
};
