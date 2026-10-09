# Publicador de Reels — @content_central_official

Publica **2 reels por dia, a horas aleatórias** (uma entre as 12:00 e as 15:00 e outra entre
as 19:00 e as 23:00, hora de Lisboa; as horas mudam todos os dias), e logo a seguir a cada
reel uma **story com os primeiros 15 s**. No primeiro dia (`start_date`) publica só 1.
Usa só a **API oficial do Instagram** (Graph API, Content Publishing): não há automação de
browser nem bibliotecas não oficiais.

```
D:\Content Central ──(update, no teu PC)──► bucket Cloudflare R2 ──► GitHub Actions ──► Instagram
   Videos\ Covers\                           (fila + estado)        (PC pode estar     reel + story
   Descriptions.txt                                                    desligado)
```

- **Ordem**: primeiro os números de `publish_first` no `config.toml` (agora 01 e 16), depois
  a numeração normal.
- **Nunca publica duplicados**: antes de cada publicação lê todas as publicações da conta e
  marca como "já publicado" o que já lá estiver, incluindo o que publicaste à mão.
- **Nunca apaga nem edita** publicações existentes, e só publica o que está na fila.
- **Pausa sozinho** depois de 2 falhas seguidas. O GitHub envia-te um email sempre que
  uma execução falha.
- A **primeira execução é sempre um ensaio** (dry-run), mesmo que não o peças.

## Horários

Configuram-se no `config.toml`, secção `[schedule]`:

| Campo | Para quê |
|---|---|
| `windows` | Janelas (hora de Lisboa); 1 reel por janela, a um minuto aleatório |
| `start_date` | Primeiro dia: só a última janela (1 vídeo). Antes disso não publica |
| `slot_seed` | Muda-a para baralhar todas as horas futuras |
| `min_hours_between_posts` | Segurança: mínimo entre 2 reels (3 h) |

Para ver as horas dos próximos dias:
`python -c "from datetime import date, timedelta; from reels_bot.config import load_settings; from reels_bot.slots import daily_slots; s = load_settings(); [print(date.today() + timedelta(d), [x.strftime('%H:%M') for x in daily_slots(date.today() + timedelta(d), s)]) for d in range(7)]"`

Se mudares as janelas, as horas do `cron` em `.github/workflows/publish.yml` têm de as
cobrir (em UTC; Lisboa é UTC+1 no verão e UTC+0 no inverno). O teste
`tests/test_slots.py` confirma isso: corre `python -m pytest` depois de mudares.

---

## Minutos do GitHub Actions

O workflow acorda de 30 em 30 min dentro das janelas (22 vezes por dia). Em ~5 s calcula se
há um slot nessa meia hora; se não houver, acaba logo. Quando há, espera até ao minuto exacto
e publica (reel + story, ~3–5 min).

**Isto só é gratuito porque o repositório é público**: em repositórios públicos os runners
normais do GitHub **não gastam minutos** da quota. **Não o tornes privado** (gastaria
~700 min/mês). O código não tem segredos: os tokens ficam nos *repo secrets* (encriptados e
escondidos nos logs) e os vídeos no bucket privado.

- Fila vazia ou pausa: o workflow desliga-se sozinho e recebes **1 email** a avisar.
- O GitHub desliga agendamentos de repositórios públicos sem commits durante 60 dias (avisa
  por email antes). Se acontecer: `gh workflow enable publish.yml`.

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
     @content_central_official.
3. **Bucket R2** (Cloudflare): cria o bucket `new-video-everyday` (**privado**) e uma API
   key "Object Read & Write" só para esse bucket. O assistente testa escrita, URL
   pré-assinado (HTTPS, válido 2 h) e remoção.

O `.env` está no `.gitignore` e **nunca** vai para o GitHub.

## 3. Importar e enviar a fila

```powershell
python -m reels_bot update      # = import + push-queue
python -m reels_bot status      # tabela: #, título, estado, quando, permalink
```

- O `import` lê `D:\Content Central` (caminho no `config.toml`):
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
quota e o intervalo mínimo entre reels, valida o próximo item e confirma que o Instagram consegue
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
A partir daí publica 2 reels por dia, a horas aleatórias dentro das janelas (ver "Horários").

## 6. Primeira publicação real

Espera pelo próximo slot, ou dispara à mão em GitHub → Actions → publish-reel →
**Run workflow**. Também podes publicar a partir do PC com
`python -m reels_bot publish-next`. Confirma no Instagram e com `status`.

---

## Juntar vídeos novos mais tarde

1. Copia `NN-slug.mp4` para `D:\Content Central\Videos` e `NN-slug-cover.jpg` para `Covers`.
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
- Mínimo **3 h** entre reels (qualquer reel da conta, manual ou do bot), e cada slot
  aleatório só é usado uma vez, mesmo que a publicação falhe.
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
| Quero mudar os horários | `windows` no `config.toml`; se saírem das horas do `cron` do workflow, ajusta-as e corre `python -m pytest` (ver "Horários") |

## Testes

```powershell
python -m pytest --cov
```
