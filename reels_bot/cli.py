"""Linha de comandos: python -m reels_bot <comando>."""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from .config import LOG_DIR, QUEUE_DIR, ConfigError, Settings, load_env_file, load_secrets, load_settings
from .graph import GraphError
from .importer import import_queue, item_errors
from .logs import setup_logging
from .publisher import EXIT_CODES, Publisher, PublishError, UsageError, active_lock
from .state import is_done, item_state, mark_skipped, record_failure, resume

log = logging.getLogger("reels_bot")


# --- helpers ----------------------------------------------------------------
def _store(settings: Settings):
    from .store import BucketStore

    return BucketStore.from_secrets(load_secrets(), settings)


def _publisher(settings: Settings, store) -> Publisher:
    from .graph import GraphClient
    from .tokens import ensure_token

    secrets = load_secrets()
    token = ensure_token(secrets, settings, store)
    return Publisher(GraphClient(token, settings.graph_api_version), store, settings, secrets.ig_user_id)


def _table(rows: list[list[str]], header: list[str]) -> str:
    widths = [max(len(str(r[i])) for r in [header, *rows]) for i in range(len(header))]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    return "\n".join([fmt.format(*header), fmt.format(*("-" * w for w in widths)),
                      *(fmt.format(*r) for r in rows)])


# --- comandos ---------------------------------------------------------------
def cmd_setup(args, settings: Settings) -> int:
    from .setup_wizard import run_setup

    run_setup(settings)
    return 0


def cmd_import(args, settings: Settings) -> int:
    src = settings.source_dir
    videos = Path(args.videos) if args.videos else src / settings.videos_subdir
    covers = Path(args.covers) if args.covers else src / settings.covers_subdir
    captions = Path(args.captions) if args.captions else src / settings.captions_file
    log.info("A importar de %s | %s | %s (as stories demoram uns segundos cada)", videos, covers, captions)
    report = import_queue(videos, covers, captions, QUEUE_DIR, settings.story_teaser_seconds)
    rows = [[i.slug, i.title[:45], "OK" if not i.errors else "; ".join(i.errors)] for i in report.items]
    print(_table(rows, ["item", "título", "validação"]))
    for warning in report.warnings:
        log.warning(warning)
    for slug in report.removed:
        log.info("Removido da fila local (já não existe na fonte): %s", slug)
    log.info("%d itens na fila local, %d com problemas.", len(report.items),
             sum(1 for i in report.items if i.errors))
    return 0


def cmd_push_queue(args, settings: Settings) -> int:
    from .store import push_queue

    report = push_queue(_store(settings), QUEUE_DIR)
    log.info("Enviados: %d ficheiros | removidos do bucket: %s | já publicados (ignorados): %d",
             len(report.uploaded), ", ".join(report.removed) or "nenhum", len(report.skipped_posted))
    return 0


def cmd_update(args, settings: Settings) -> int:
    args.videos = args.covers = args.captions = None
    return cmd_import(args, settings) or cmd_push_queue(args, settings)


def cmd_sync(args, settings: Settings) -> int:
    store = _store(settings)
    _, marked = _publisher(settings, store).sync()
    log.info("Marcados como já publicados: %s", ", ".join(marked) or "nenhum novo")
    return 0


def cmd_status(args, settings: Settings) -> int:
    store = _store(settings)
    state = store.load_state()
    rows = []
    for item in store.load_items():
        entry = item_state(state, item.slug)
        if entry.get("posted"):
            story = " +story" if entry.get("story_media_id") else (" (story falhou)" if entry.get("story_error") else "")
            what = f"publicado ({entry.get('source')}){story}"
        elif entry.get("skipped"):
            what = f"saltado: {entry.get('skip_reason', '')}"
        else:
            errors = item_errors(item.caption, item.meta, item.has_video, item.has_cover, item.has_story)
            what = "na fila" if not errors else "INVÁLIDO: " + "; ".join(errors)
        rows.append([item.slug.split("-")[0], item.title[:40], what,
                     (entry.get("posted_at") or "")[:16], entry.get("permalink") or ""])
    print(_table(rows, ["#", "título", "estado", "quando", "permalink"]))
    if state["paused"]:
        print(f"\n⚠ PAUSADO: {state['pause_reason']}  →  `python -m reels_bot resume`")
    if not state["dry_run_ok_at"]:
        print("\nℹ Ainda não fizeste o primeiro dry-run: `python -m reels_bot publish-next --dry-run`")
    return 0


def _publish(args, settings: Settings, slug: str | None) -> int:
    from .tokens import TokenError

    store = _store(settings)
    state = store.load_state()
    if state["paused"]:
        log.error("Bot em pausa: %s. Corrige a causa e corre `resume`.", state["pause_reason"])
        return EXIT_CODES["paused"]
    try:
        publisher = _publisher(settings, store)
    except TokenError as exc:
        log.error("%s", exc)
        if args.dry_run or not state["dry_run_ok_at"]:  # ensaio (pedido ou forçado) não conta
            return EXIT_CODES["failed"]
        state = record_failure(state, str(exc), settings.max_consecutive_failures)
        store.save_state(state)
        return EXIT_CODES["paused" if state["paused"] else "failed"]
    result = publisher.run(dry_run=args.dry_run, slug=slug)
    log.info("Resultado: %s | %s | %s", result.outcome, result.slug or "-", result.permalink or result.reason)
    return result.exit_code


def cmd_publish_next(args, settings: Settings) -> int:
    return _publish(args, settings, None)


def cmd_publish(args, settings: Settings) -> int:
    return _publish(args, settings, args.slug)


def _refuse_if_publishing(store) -> None:
    held = active_lock(store, datetime.now(UTC))
    if held:
        raise UsageError(f"Há uma publicação em curso ({held.get('owner')}, desde {held['at']}). "
                         "Tenta daqui a uns minutos.")


def cmd_skip(args, settings: Settings) -> int:
    store = _store(settings)
    _refuse_if_publishing(store)
    state = store.load_state()
    if args.slug not in store.list_slugs():
        raise UsageError(f"'{args.slug}' não existe na fila do bucket")
    if is_done(state, args.slug):
        raise UsageError(f"'{args.slug}' já está publicado ou saltado")
    store.save_state(mark_skipped(state, args.slug, args.reason))
    log.info("Saltado: %s (%s)", args.slug, args.reason)
    return 0


def cmd_resume(args, settings: Settings) -> int:
    store = _store(settings)
    _refuse_if_publishing(store)
    store.save_state(resume(store.load_state()))
    log.info("Pausa levantada. Se o workflow do GitHub foi desactivado, reactiva-o com:\n"
             "    gh workflow enable publish.yml")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m reels_bot", description="Publicador de Reels @new.video.everyday")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup", help="configurar tokens do Meta e bucket R2").set_defaults(func=cmd_setup)
    p = sub.add_parser("import", help="criar/espelhar queue/ a partir da pasta-fonte (ou de ZIPs)")
    p.add_argument("videos", nargs="?", help="ZIP ou pasta de vídeos (omissão: config.toml)")
    p.add_argument("covers", nargs="?", help="ZIP ou pasta de capas")
    p.add_argument("captions", nargs="?", help=".txt de legendas")
    p.set_defaults(func=cmd_import)
    sub.add_parser("push-queue", help="enviar queue/ para o bucket").set_defaults(func=cmd_push_queue)
    sub.add_parser("update", help="import + push-queue (depois de juntar vídeos novos)").set_defaults(func=cmd_update)
    sub.add_parser("sync", help="detectar reels já publicados").set_defaults(func=cmd_sync)
    sub.add_parser("status", help="tabela da fila").set_defaults(func=cmd_status)
    p = sub.add_parser("publish-next", help="publicar o próximo da fila")
    p.add_argument("--dry-run", action="store_true", help="validar tudo sem publicar")
    p.set_defaults(func=cmd_publish_next)
    p = sub.add_parser("publish", help="publicar um item específico")
    p.add_argument("slug")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_publish)
    p = sub.add_parser("skip", help="nunca publicar este item")
    p.add_argument("slug")
    p.add_argument("--reason", default="saltado manualmente")
    p.set_defaults(func=cmd_skip)
    sub.add_parser("resume", help="levantar a pausa automática").set_defaults(func=cmd_resume)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings()
        setup_logging(LOG_DIR, settings.log_keep_days)
        load_env_file()
        return args.func(args, settings)
    except (ConfigError, UsageError, FileNotFoundError, ValueError) as exc:
        log.error("%s", exc)
        return 2
    except (GraphError, PublishError) as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        return 130
