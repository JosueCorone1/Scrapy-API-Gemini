"""Interactive Instagram export with an optional Gemini language analysis.

Only data visible to the current Instagram session is collected. Selected
post/reel media files and the report are written to output/.
"""
import argparse
import csv
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import config
from instagram import refs
from instagram.fetch import InstagramUpstreamError
from instagram.media_download import download_content
from instagram.users import (
    get_followers,
    get_following,
    get_posts,
    get_profile,
    get_reels,
    resolve as resolve_profile,
)
from gemini_analysis import analyze_texts

OUTPUT_DIR = Path(__file__).resolve().parent / "output"

HASHTAG_RE = re.compile(r"(?<![\w])#[\w]+", re.UNICODE)
MENTION_RE = re.compile(r"(?<![\w.])@[\w.]+", re.UNICODE)


def save_report(username, report):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"instagram_report_{username}.json"
    with path.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    return path


def collect_all_pages(fetch_page, *, item_key, count_key, label, limit=None, progress=None):
    """Follow cursors until the list ends, fails, or the requested limit is met."""
    items = []
    seen_items = set()
    seen_cursors = set()
    first_page = None
    pending_cursor = None
    cursor = None
    pages_fetched = 0
    complete = False
    limited = False
    error = None

    while True:
        try:
            page = fetch_page(cursor)
            if not isinstance(page, dict):
                raise TypeError("Instagram devolvió una página no válida")
        except Exception as exc:
            if isinstance(exc, InstagramGated):
                error = (
                    "Instagram rechazó la sesión para esta lista (login_required/HTTP 401). "
                    "Renueva INSTAGRAM_COOKIES desde una sesión autorizada que pueda ver la lista."
                )
            elif isinstance(exc, InstagramBlocked):
                error = (
                    "Instagram limitó temporalmente la solicitud (HTTP 403/429). "
                    "Espera antes de volver a consultar."
                )
            else:
                error = f"{type(exc).__name__}: {str(exc)[:250]}"
            break

        if first_page is None:
            first_page = page
        pages_fetched += 1
        rows = page.get(item_key) or []
        added = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = row.get("id") or row.get("username")
            if key is None:
                key = json.dumps(row, sort_keys=True, ensure_ascii=False)
            if key in seen_items:
                continue
            seen_items.add(key)
            items.append(row)
            added += 1
            if limit is not None and len(items) >= limit:
                break

        limit_hit = limit is not None and len(items) >= limit
        if limit_hit:
            limited = bool(page.get("has_more")) or len(rows) > added
            complete = not limited
            pending_cursor = None
            break

        if not page.get("has_more"):
            complete = True
            pending_cursor = None
            break

        pending_cursor = page.get("next_cursor")
        if not pending_cursor:
            error = "Instagram indicó que había más páginas, pero no entregó un cursor."
            break
        if pending_cursor in seen_cursors:
            error = "Instagram repitió el cursor; se detuvo para evitar un bucle."
            break
        if not rows:
            error = "Instagram indicó que había más páginas, pero la página llegó vacía."
            break

        seen_cursors.add(pending_cursor)
        cursor = pending_cursor

    result = dict(first_page or {})
    result[item_key] = items
    result[count_key] = len(items)
    result["pages_fetched"] = pages_fetched
    result["complete"] = complete
    result["limited"] = limited
    result["has_more"] = not complete
    result["next_cursor"] = pending_cursor if not complete and not limited else None
    result["status"] = (
        "limit_reached" if limited else
        "complete" if complete else
        "partial" if items else
        "failed"
    )
    if error:
        result["error"] = error
    print(f"{label}: {len(items)} elementos ({result['status']})")
    if error:
        print(f"{label}: {error}")
    if progress:
        progress(f"{label}: {len(items)} elementos ({result['status']})")
    return result


def prompt_inputs():
    while True:
        raw_username = input("¿A qué usuario deseas hacerle el scraping? (puedes incluir @): ").strip()
        try:
            user = refs.resolve_user(raw_username)
            if not user.get("username"):
                raise ValueError("Escribe un @handle, no un ID numérico.")
            break
        except ValueError as exc:
            print(f"Usuario no válido: {exc}")

    while True:
        raw_limit = input("¿Cuántas publicaciones/reels deseas extraer? ").strip()
        try:
            limit = int(raw_limit)
            if limit < 1:
                raise ValueError
            break
        except ValueError:
            print("Introduce un número entero mayor que cero.")

    return user, limit


def _content_record(item, source):
    caption = item.get("caption") or ""
    stats = item.get("stats") or {}
    return {
        "id": item.get("id"),
        "source": source,
        "_media": {
            "image": item.get("image"),
            "video": item.get("video"),
            "carousel": item.get("carousel") or [],
        },
        "format": item.get("type") or item.get("product_type") or "unknown",
        "product_type": item.get("product_type"),
        "url": item.get("link"),
        "date": item.get("published_at"),
        "caption": caption,
        "hashtags": list(dict.fromkeys(HASHTAG_RE.findall(caption))),
        "mentions": list(dict.fromkeys(MENTION_RE.findall(caption))),
        "metrics": {
            "likes": stats.get("like_count"),
            "views": stats.get("view_count") or stats.get("play_count"),
            "comments": stats.get("comment_count"),
        },
    }


def _analysis_input(profile, content):
    chunks = []
    biography = profile.get("biography") or ""
    if biography.strip():
        chunks.append(f"Biografía:\n{biography}")
    for index, item in enumerate(content, 1):
        caption = item.get("caption") or ""
        if caption.strip():
            chunks.append(f"Texto {index} ({item.get('source')}, {item.get('date') or 'fecha desconocida'}):\n{caption}")
    return "\n\n".join(chunks)


def _confirm_gemini_upload():
    answer = input(
        "Gemini recibirá la biografía y las descripciones (sin metadatos del perfil). "
        "¿Confirmas que tienes autorización para este análisis y deseas enviarlas? [s/N] "
    ).strip().casefold()
    return answer in {"s", "si", "sí", "y", "yes"}


def _gemini_failure_message(exc):
    message = str(exc)
    api_key = config.gemini_api_key()
    if api_key:
        message = message.replace(api_key, "[clave ocultada]")
    message = re.sub(r"(?i)([?&]key=)[^&\s'\"}]+", r"\1[clave ocultada]", message)
    lower = message.casefold()
    if isinstance(exc, TimeoutError) or "timeout" in lower or "tiempo de espera" in lower:
        return (
            "Gemini agotó el tiempo de espera. Comprueba la conexión, proxy o firewall; "
            "puedes reintentar el análisis guardado sin volver a consultar Instagram."
        )
    if "deadline_exceeded" in lower or "http 504" in lower or "504 gateway" in lower:
        return (
            "Gemini no terminó la solicitud antes del límite del servidor. "
            "Prueba de nuevo más tarde o selecciona un modelo Flash más ligero."
        )
    if any(marker in lower for marker in ("api key", "api_key", "unauthorized", "http 401", "http 403")):
        return (
            "Gemini rechazó la clave o sus permisos. Crea una clave nueva en Google AI Studio, "
            "confirma acceso a Gemini API y actualiza GEMINI_API_KEY en .env."
        )
    if "http 429" in lower or "resource_exhausted" in lower or "quota" in lower:
        return "Gemini alcanzó el límite de uso/cuota; espera o revisa la cuota del proyecto."
    return f"Gemini falló ({type(exc).__name__}): {message[:500]}"


def reanalyze_saved_report(raw_username):
    """Retry Gemini from a saved report without sending more requests to Instagram."""
    try:
        user = refs.resolve_user(raw_username)
    except ValueError as exc:
        raise SystemExit(f"Handle no válido: {exc}") from exc
    username = user.get("username")
    if not username:
        raise SystemExit("Escribe el handle de Instagram, no un ID numérico.")
    path = OUTPUT_DIR / f"instagram_report_{username}.json"
    if not path.is_file():
        raise SystemExit(f"No existe un informe guardado para @{username}: {path}")
    if not config.gemini_api_key():
        raise SystemExit("Falta GEMINI_API_KEY en .env.")

    with path.open("r", encoding="utf-8") as handle:
        report = json.load(handle)
    profile = report.get("profile") or {}
    items = (report.get("content") or {}).get("items") or []
    print(f"Se usarán {len(items)} publicaciones/reels del informe local; no se consultará Instagram.")
    if not _analysis_input(profile, items).strip():
        raise SystemExit("El informe no contiene biografía ni descripciones para analizar.")
    if not _confirm_gemini_upload():
        print("Análisis cancelado; el informe no se modificó.")
        return

    analysis = report.setdefault("personality_analysis", {})
    analysis.update({
        "status": "failed",
        "provider": "Gemini",
        "model": config.gemini_model(),
        "result": None,
    })
    try:
        analysis["result"] = analyze_texts(
            profile, items, api_key=config.gemini_api_key(), model=config.gemini_model(),
        )
        analysis["status"] = "complete"
        analysis.pop("error", None)
    except Exception as exc:
        analysis["error"] = _gemini_failure_message(exc)
        print(analysis["error"])

    path = save_report(username, report)
    print(f"Informe actualizado: {path}")
    print(f"Análisis Gemini: {analysis['status']}")


def run_export(raw_username, limit, *, gemini_consent=None, progress=None):
    """Run one export and return (report, path); GUI and CLI share this flow."""
    user = refs.resolve_user(raw_username)
    if not user.get("username"):
        raise ValueError("Escribe un @handle, no un ID numérico.")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("El límite debe ser un entero mayor que cero.")
    username = user["username"]

    def report_progress(message):
        print(message)
        if progress:
            progress(message)

    if not config.instagram_cookies():
        raise RuntimeError(
            "Falta INSTAGRAM_COOKIES. Configura una cookie de tu propia sesión en el archivo .env."
        )

    report_progress(f"Consultando @{username}...")
    try:
        profile = get_profile(user)
    except InstagramBlocked as exc:
        detail = str(exc)
        if "429" in detail:
            raise SystemExit(
                "Instagram aplicó un límite temporal (HTTP 429) al consultar el perfil. "
                "Espera antes de volver a ejecutar; el informe guardado no se modificó."
            ) from None
        raise SystemExit(
            f"Instagram bloqueó la consulta del perfil ({detail}). "
            "El informe guardado no se modificó."
        ) from None
    except InstagramGated as exc:
        raise SystemExit(
            f"Instagram requiere una sesión válida para consultar el perfil ({exc}). "
            "El informe guardado no se modificó."
        ) from None

    posts = collect_all_pages(
        lambda cursor: get_posts(user, cursor=cursor),
        item_key="posts", count_key="post_count", label="Publicaciones", limit=limit,
        progress=progress,
    )
    reels = collect_all_pages(
        lambda cursor: get_reels(user, cursor=cursor),
        item_key="reels", count_key="reel_count", label="Reels", limit=limit,
        progress=progress,
    )
    records = [_content_record(row, "publicación") for row in posts["posts"]]
    records.extend(_content_record(row, "reel") for row in reels["reels"])
    unique_content = []
    seen_content = set()
    for row in records:
        key = row.get("id") or row.get("url") or (row.get("date"), row.get("caption"))
        if key in seen_content:
            continue
        seen_content.add(key)
        unique_content.append(row)
    unique_content.sort(key=lambda row: row.get("date") or "", reverse=True)
    content = unique_content[:limit]
    report_progress("Descargando fotos y videos seleccionados...")
    download_content(username, content, OUTPUT_DIR)
    for item in content:
        status = (item.get("media_downloads") or {}).get("status", "unavailable")
        print(f"Medios de {item.get('url') or item.get('id')}: {status}")
        item.pop("_media", None)

    analysis = {
        "status": "skipped_missing_api_key",
        "provider": "Gemini",
        "model": config.gemini_model(),
        "result": None,
        "note": (
            "Las puntuaciones son impresiones lingüísticas especulativas, no una prueba "
            "psicométrica validada ni un diagnóstico."
        ),
    }
    if config.gemini_api_key():
        if not _analysis_input(profile, content).strip():
            analysis["status"] = "skipped_no_text"
        elif gemini_consent is None or gemini_consent:
            if gemini_consent is None and not _confirm_gemini_upload():
                analysis["status"] = "skipped_without_confirmation"
            else:
                report_progress("Analizando los textos con Gemini...")
                try:
                    analysis["result"] = analyze_texts(
                        profile, content, api_key=config.gemini_api_key(),
                        model=config.gemini_model(),
                    )
                    analysis["status"] = "complete"
                except Exception as exc:
                    analysis["status"] = "failed"
                    analysis["error"] = _gemini_failure_message(exc)
                    print(analysis["error"])
        else:
            analysis["status"] = "skipped_without_confirmation"

    report = {
        "target": username,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "profile": {
            "username": profile.get("username") or username,
            "full_name": profile.get("full_name"),
            "biography": profile.get("biography"),
            "links": {
                "bio": profile.get("bio_links") or [],
                "external": profile.get("external_link"),
            },
            "category": profile.get("category"),
            "counts": {
                "posts": profile.get("post_count"),
                "followers": profile.get("follower_count"),
                "following": profile.get("following_count"),
            },
            "is_private": profile.get("is_private"),
        },
        "connections": {
            "followers_count": profile.get("follower_count"),
            "following_count": profile.get("following_count"),
            "source": "profile_metadata",
        },
        "content": {
            "requested_limit": limit,
            "count": len(content),
            "items": content,
            "collection": {
                "posts": {key: posts.get(key) for key in ("complete", "status", "error")},
                "reels": {key: reels.get(key) for key in ("complete", "status", "error")},
            },
        },
        "personality_analysis": analysis,
    }
    path = save_report(username, report)
    print(f"\nInforme guardado en: {path}")
    print(f"Elementos de contenido incluidos: {len(content)} de {limit}")
    print(f"Seguidores: {profile.get('follower_count') if profile.get('follower_count') is not None else 'no disponible'}")
    print(f"Seguidos: {profile.get('following_count') if profile.get('following_count') is not None else 'no disponible'}")
    print(f"Análisis Gemini: {analysis['status']}")
    report_progress(f"Informe guardado: {path}")
    return report, path



def run_benford_scan(raw_username, *, progress=None):
    """Collect visible relationships and individual profile follower counts.

    Partial results are preserved. A login gate or 403/429 stops subsequent
    requests instead of retrying or changing the session/network route.
    """
    target = refs.resolve_user(raw_username)
    if not target.get("username"):
        raise ValueError("Escribe el @handle de Instagram, no un ID numérico.")
    if not config.instagram_cookies():
        raise RuntimeError(
            "Falta INSTAGRAM_COOKIES. Configura una cookie de tu propia sesión en .env."
        )

    username = target["username"]

    def report_progress(message):
        print(message)
        if progress:
            progress(message)

    def collect(label, fetch_page):
        report_progress(f"Consultando {label.lower()} visibles de @{username}...")
        return collect_all_pages(
            fetch_page,
            item_key="users",
            count_key="user_count",
            label=label,
            progress=progress,
        )

    followers = collect(
        "Seguidores", lambda cursor: get_followers(target, cursor=cursor)
    )
    block_markers = ("429", "403", "login_required", "limitó temporalmente")
    blocked = any(
        marker in (followers.get("error") or "").lower()
        for marker in block_markers
    )

    if blocked:
        following = {
            "users": [],
            "user_count": 0,
            "complete": False,
            "status": "not_requested",
            "error": "Consulta detenida para respetar el bloqueo o requisito de sesión.",
        }
    else:
        following = collect(
            "Seguidos", lambda cursor: get_following(target, cursor=cursor)
        )
        blocked = any(
            marker in (following.get("error") or "").lower()
            for marker in block_markers
        )

    unique_rows = {}
    for rows in (followers.get("users", []), following.get("users", [])):
        for row in rows:
            key = str(row.get("id") or row.get("username") or "").lower()
            if key:
                unique_rows.setdefault(key, row)

    enriched_by_key = {}
    profile_rows = list(unique_rows.items())
    interrupted = blocked
    for index, (key, row) in enumerate(profile_rows, start=1):
        enriched = dict(row)
        if interrupted:
            enriched["stats_error"] = (
                "No consultado porque Instagram limitó la recopilación."
            )
            enriched_by_key[key] = enriched
            continue

        if index == 1 or index % 10 == 0 or index == len(profile_rows):
            report_progress(
                f"Consultando conteos de perfiles: {index}/{len(profile_rows)} "
                f"(@{row.get('username') or key})..."
            )
        try:
            profile = resolve_profile(
                {"id": row.get("id"), "username": row.get("username")}
            )
            enriched.update({
                "full_name": profile.get("full_name") or row.get("full_name"),
                "follower_count": profile.get("follower_count"),
                "following_count": profile.get("following_count"),
                "is_private": profile.get("is_private", row.get("is_private")),
                "is_verified": profile.get("is_verified", row.get("is_verified")),
                "link": profile.get("link") or row.get("link"),
            })
            if (
                enriched["follower_count"] is None
                or enriched["following_count"] is None
            ):
                enriched["stats_error"] = (
                    "Instagram no proporcionó ambos conteos del perfil."
                )
        except InstagramUpstreamError as exc:
            enriched["follower_count"] = None
            enriched["following_count"] = None
            enriched["stats_error"] = f"Consulta detenida: {str(exc)[:180]}"
            interrupted = True
        except Exception as exc:
            enriched["follower_count"] = None
            enriched["following_count"] = None
            enriched["stats_error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
        enriched_by_key[key] = enriched

    def enrich_rows(rows):
        result_rows = []
        for row in rows:
            key = str(row.get("id") or row.get("username") or "").lower()
            result_rows.append(enriched_by_key.get(key, dict(row)))
        return result_rows

    result = {
        "target": username,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "followers": enrich_rows(followers.get("users", [])),
        "following": enrich_rows(following.get("users", [])),
        "collection": {
            "followers": {
                key: followers.get(key)
                for key in ("complete", "status", "error", "pages_fetched")
            },
            "following": {
                key: following.get(key)
                for key in ("complete", "status", "error", "pages_fetched")
            },
            "metrics_interrupted": interrupted,
        },
    }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = OUTPUT_DIR / f"benford_scan_{username}.csv"
    json_path = OUTPUT_DIR / f"benford_scan_{username}.json"
    fields = (
        "relationship", "username", "full_name", "follower_count",
        "following_count", "is_private", "is_verified", "link", "stats_error",
    )
    with csv_path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for relationship in ("followers", "following"):
            for row in result[relationship]:
                writer.writerow({"relationship": relationship, **row})

    result["files"] = {
        "csv": str(csv_path.resolve()),
        "json": str(json_path.resolve()),
    }
    with json_path.open("w", encoding="utf-8") as output:
        json.dump(result, output, ensure_ascii=False, indent=2)

    report_progress(
        f"Extracción terminada: {len(result['followers'])} seguidores y "
        f"{len(result['following'])} seguidos. CSV: {csv_path}"
    )
    return result

def main():
    print("Exportador de perfil de Instagram")
    print("Usa solo datos visibles en tu sesión y perfiles que tengas autorización para analizar.\n")
    user, limit = prompt_inputs()
    run_export(user["username"], limit)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Exporta un perfil de Instagram.")
    parser.add_argument(
        "--reanalyze",
        metavar="HANDLE",
        help="reintenta Gemini con el informe local, sin consultar Instagram",
    )
    args = parser.parse_args()
    if args.reanalyze:
        reanalyze_saved_report(args.reanalyze)
    else:
        main()
