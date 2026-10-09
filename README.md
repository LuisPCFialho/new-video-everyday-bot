# Publicador de Reels — @new.video.everyday

Publica **1 reel por noite às 21:00 (Lisboa)** a partir de uma fila, e logo a seguir
uma **story com os primeiros 15 s** desse reel. Usa só a **API oficial do Instagram**
(Graph API, Content Publishing): não há automação de browser nem bibliotecas não oficiais.

```
D:\new.video.everyday ──(update, no teu PC)──► bucket Cloudflare R2 ──► GitHub Actions 21:00 ──► Instagram
   Videos\ Covers\                              (fila + estado)            (PC pode estar desligado)   reel + story
   Descriptions.txt
```

- **Nunca publica duplicados**: antes de cada publicação lê todos os reels da conta e
  marca como "já publicado" o que já lá estiver, incluindo os que publicaste à mão.
- **Nunca apaga nem edita** publicações existentes, e só publica o que está na fila.
- **Pausa sozinho** depois de 2 falhas seguidas. O GitHub envia-te um email sempre que
  uma execução falha.
- A **primeira execução é sempre um ensaio** (dry-run), mesmo que não o peças.

---

## Minutos do GitHub Actions

| Situação | Minutos |
|---|---|
| Noite com publicação (reel + story) | ~3–5 min (a maior parte é à espera que o Instagram processe o vídeo) |
| Noite em que ainda não passaram 20 h desde o último reel | ~1 min |
| Semanas de mudança de hora (14 dias/ano) | +1 min nesses dias (há um disparo extra que sai em segundos) |
| Fila vazia ou bot em pausa | **0**: o workflow desliga-se sozinho e recebes **1 email** a avisar |
| Commits/push para o repositório | **0**: o workflow não corre em push |

Para 16 vídeos são cerca de **60–80 min no total**. Duas opções para não tocares na quota
dos teus negócios:

1. **Repositório público** (recomendado): em repositórios públicos os runners normais do
   GitHub **não gastam minutos** da quota. O código não tem segredos: os tokens ficam nos
   *repo secrets* (encriptados e escondidos nos logs) e os vídeos no bucket privado. Os logs
   das execuções ficam visíveis (títulos e legendas, que já são públicos no Instagram).
   Nota: o GitHub desliga agendamentos de repositórios públicos sem actividade durante
   60 dias e avisa por email antes. Se acontecer, basta reactivar (ver "Problemas").
2. **Repositório privado na tua conta pessoal**: os minutos saem da quota da tua conta
   pessoal (2 000 min/mês no plano gratuito), não da quota das organizações das empresas.

---

## 1. Instalar (uma vez)

Já tens Python 3.13, ffmpeg, Git e GitHub CLI nesta máquina. Abre o **PowerShell**:

```powershell
cd C:\Apps\ig-reels-publisher
py -3.13 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
```

Sempre que abrires um PowerShell novo para usar o bot, corre primeiro:
`cd C:\Apps\ig-reels-publisher` e `.venv\Scripts\Activate.ps1`.

## 2. Configurar contas (`setup`)

```powershell
python -m reels_bot setup
```

O assistente guia-te passo a passo e testa cada coisa antes de a gravar no `.env`:

1. **App do Meta**: em developers.facebook.com cria uma app do tipo **Empresa
   (Business)** e adiciona o produto **Instagram** ("API setup with Facebook login").
   Pode ficar em modo de desenvolvimento, porque és administrador dela.
2. **Token** com as permissões `instagram_basic`, `instagram_content_publish`,
   `pages_show_list`, `pages_read_engagement` e `business_management`:
   - **Opção A, recomendada**: token de **System User** em business.facebook.com, com
     expiração **"Nunca"**. Não precisa de renovação.
   - **Opção B**: token do Graph API Explorer. O assistente troca-o por um de 60 dias e o
     bot renova-o sozinho quando faltarem menos de 10 dias. Se a renovação falhar, a
     execução fica a vermelho e recebes o email do GitHub.
   - O assistente descobre sozinho a Página, o *Page access token* e o **IG_USER_ID** de
     @new.video.everyday.
3. **Bucket R2** (Cloudflare): cria o bucket `new-video-everyday` (**privado**) e uma API
   key "Object Read & Write" só para esse bucket. O assistente testa escrita, URL
   pré-assinado (HTTPS, válido 2 h) e remoção.

O `.env` está no `.gitignore` e **nunca** vai para o GitHub.

## 3. Importar e enviar a fila

```powershell
python -m reels_bot update      # = import + push-queue
python -m reels_bot status      # tabela: #, título, estado, quando, permalink
```

- O `import` lê `D:\new.video.everyday` (caminho no `config.toml`):
  `Videos\NN-slug.mp4`, `Covers\NN-slug-cover.jpg` e `Descriptions.txt`. Valida cada vídeo
  (3–90 s, 9:16, ≤ 1 GB, H.264 + AAC), cada legenda (≤ 2 200 caracteres, ≤ 30 hashtags) e
  gera `story.mp4` (primeiros 15 s).
- A linha `NN · Título` do `Descriptions.txt` **não** é publicada: serve de título interno.
  A legenda publicada é o resto do bloco.
- O `push-queue` envia para o bucket só o que mudou. Itens já publicados não são tocados.
- Também aceita ZIPs: `python -m reels_bot import videos.zip covers.zip legendas.txt`.

## 4. Primeiro ensaio (obrigatório)

```powershell
python -m reels_bot publish-next --dry-run
```

O ensaio faz tudo menos publicar: detecta os reels que já publicaste à mão, verifica a
quota e o intervalo de 20 h, valida o próximo item e confirma que o Instagram consegue
descarregar os ficheiros do bucket. Mostra a legenda que seria publicada.
**Enquanto não houver um ensaio com sucesso, qualquer publicação é convertida em ensaio.**

## 5. Ligar o agendamento no GitHub

```powershell
git add -A
git commit -m "feat: publicador de reels"
gh repo create new-video-everyday-bot --public --source . --push
gh secret set -f .env
```

Depois, em GitHub → **Actions**, confirma que o workflow **publish-reel** está activo.
A partir daí corre todas as noites às 21:00 de Lisboa (o GitHub pode atrasar 5–30 min).

## 6. Primeira publicação real

Espera pelas 21:00, ou dispara à mão em GitHub → Actions → publish-reel →
**Run workflow**. Também podes publicar a partir do PC com
`python -m reels_bot publish-next`. Confirma no Instagram e com `status`.

---

## Juntar vídeos novos mais tarde

1. Copia `NN-slug.mp4` para `D:\new.video.everyday\Videos` e `NN-slug-cover.jpg` para `Covers`.
2. Acrescenta o bloco ao `Descriptions.txt`:
   ```
   ========================================

   17 · What if ...?

   Texto da legenda...

   #whatif #...
   ```
3. Corre `python -m reels_bot update`.
4. Se a fila tinha acabado, o workflow desligou-se: `gh workflow enable publish.yml`.

A pasta `D:` é a fonte de verdade. Se mudares o nome (slug) de um vídeo ainda não
publicado, o `update` remove o item antigo da fila. Para corrigir uma legenda ainda não
publicada, edita o `Descriptions.txt` e corre `update`.

## Comandos

| Comando | O que faz |
|---|---|
| `setup` | Configura tokens do Meta e bucket R2 |
| `import [videos covers legendas]` | Constrói `queue/` a partir da pasta D: (ou de ZIPs) |
| `push-queue` | Envia `queue/` para o bucket |
| `update` | `import` + `push-queue` |
| `sync` | Marca como publicados os reels que já estão no Instagram |
| `status` | Tabela da fila |
| `publish-next [--dry-run]` | Publica o próximo item válido (reel + story) |
| `publish <slug> [--dry-run]` | Publica um item específico (ex.: `publish 05-mariana-trench-dive`) |
| `skip <slug> [--reason "..."]` | Nunca publicar este item |
| `resume` | Levanta a pausa automática |

Opcional: em `queue\<item>\meta.json`, `"scheduled_for": "2026-12-24"` impede a
publicação antes dessa data. Corre `push-queue` depois de editar.

## Regras de segurança

- Antes de cada publicação: sincroniza com o Instagram (todas as publicações da conta, não
  só reels) e salta o que já lá está. Cada publicação existente só "conta" para um item.
- **Lock** no bucket: se publicares a partir do PC enquanto o GitHub está a publicar (ou
  vice-versa), a segunda execução não faz nada. Nesse período `skip` e `resume` também esperam.
- Mínimo **20 h** entre reels (qualquer reel da conta, manual ou do bot). São 20 h e não
  24 h porque o GitHub atrasa os agendamentos: com 24 h, um atraso fazia perder o dia seguinte.
- Respeita a quota da API (`content_publishing_limit`).
- Erros de rede e 5xx: até 3 tentativas com espera crescente. O `media_publish` **nunca**
  é repetido sem antes confirmar que não foi publicado.
- Se uma execução morrer a meio, a seguinte verifica se o reel chegou a sair. Se não
  conseguir verificar, não publica nada nessa noite (e conta como falha).
- Itens inválidos (ex.: o **14-titanic-3d** ainda não tem vídeo) são saltados e o motivo
  fica no log. A fila avança para o seguinte.
- Logs em `logs\AAAA-MM-DD.log` (no PC; guarda 30 dias) e no separador Actions do GitHub.

## Problemas

| Sintoma | O que fazer |
|---|---|
| Email do GitHub "Run failed" | Abre a execução em Actions: o passo **Resultado** diz o motivo, o **Publicar** tem o detalhe |
| "Fila vazia" | Junta vídeos, `python -m reels_bot update` e `gh workflow enable publish.yml` |
| `status` diz **PAUSADO** | Corrige a causa, depois `python -m reels_bot resume` e `gh workflow enable publish.yml` |
| "story falhou" | O reel saiu; só a story falhou (não conta para a pausa). Podes publicar a story à mão |
| "Token inválido / Renovação falhou" | `python -m reels_bot setup` e depois `gh secret set -f .env` |
| Workflow desactivado (fila vazia ou 60 dias sem actividade) | `gh workflow enable publish.yml` |
| Quero mudar a hora | `publish_time` no `config.toml` **e** as horas dos `cron` + `PUBLISH_TIME` no workflow |

## Testes

```powershell
python -m pytest --cov
```
